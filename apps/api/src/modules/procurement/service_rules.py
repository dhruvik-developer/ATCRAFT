"""Pure procurement rules: arithmetic, state graphs, duplicate matching, ranking.

Everything in this file is a function of its arguments. No session, no I/O, no
clock beyond what the caller passes in. That is deliberate and structural rather
than stylistic: the three rules this module is judged on - the over-receipt
tolerance (master rules section 17), duplicate purchase order prevention
(section 16) and the quotation recommendation (section 15) - are all decisions
that must be exercisable, and arguable with the business, without a database.

``service.py`` imports from here and does the persistence. Nothing here imports
``service.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from src.core.errors import BusinessRuleError, ErrorCode, ValidationError
from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_money, to_quantity
from src.modules.procurement.models import (
    DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
    OVER_RECEIPT_MULTIPLIER,
    OVER_RECEIPT_TOLERANCE,
    GoodsReceiptStatus,
    PurchaseOrderLineStatus,
    PurchaseOrderStatus,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    RequisitionLineStatus,
    RequisitionStatus,
    RfqStatus,
)
from src.modules.vendors.models import MAX_VENDOR_RATING

# =============================================================================
# Document state machines - the only things permitted to move a status column
# =============================================================================

REQUISITION_LIFECYCLE: StateMachine[RequisitionStatus] = StateMachine(
    entity="Purchase requisition",
    initial=RequisitionStatus.DRAFT,
    transitions=(
        Transition(
            RequisitionStatus.DRAFT,
            RequisitionStatus.PENDING_APPROVAL,
            permission=Permission.PURCHASE_REQUISITION_CREATE,
            label="Submit for approval",
        ),
        Transition(
            RequisitionStatus.DRAFT,
            RequisitionStatus.CANCELLED,
            permission=Permission.PURCHASE_REQUISITION_CREATE,
            label="Cancel",
        ),
        Transition(
            RequisitionStatus.PENDING_APPROVAL,
            RequisitionStatus.APPROVED,
            permission=Permission.PURCHASE_REQUISITION_APPROVE,
            label="Approve",
        ),
        Transition(
            RequisitionStatus.PENDING_APPROVAL,
            RequisitionStatus.REJECTED,
            permission=Permission.PURCHASE_REQUISITION_APPROVE,
            label="Reject",
        ),
        Transition(
            RequisitionStatus.PENDING_APPROVAL,
            RequisitionStatus.CANCELLED,
            permission=Permission.PURCHASE_REQUISITION_CREATE,
            label="Withdraw",
        ),
        Transition(
            RequisitionStatus.APPROVED,
            RequisitionStatus.CONVERTED,
            permission=Permission.PURCHASE_ORDER_CREATE,
            label="Converted to an RFQ or order",
        ),
        Transition(
            RequisitionStatus.APPROVED,
            RequisitionStatus.CLOSED,
            permission=Permission.PURCHASE_REQUISITION_APPROVE,
            label="Close",
        ),
        Transition(
            RequisitionStatus.APPROVED,
            RequisitionStatus.CANCELLED,
            permission=Permission.PURCHASE_REQUISITION_APPROVE,
            label="Cancel",
        ),
        Transition(
            RequisitionStatus.CONVERTED,
            RequisitionStatus.CLOSED,
            permission=Permission.PURCHASE_REQUISITION_APPROVE,
            label="Close",
        ),
    ),
    terminal=frozenset(
        {RequisitionStatus.CLOSED, RequisitionStatus.CANCELLED, RequisitionStatus.REJECTED}
    ),
)

RFQ_LIFECYCLE: StateMachine[RfqStatus] = StateMachine(
    entity="RFQ",
    initial=RfqStatus.DRAFT,
    transitions=(
        Transition(
            RfqStatus.DRAFT, RfqStatus.SENT, permission=Permission.RFQ_MANAGE, label="Send"
        ),
        Transition(
            RfqStatus.DRAFT, RfqStatus.CANCELLED, permission=Permission.RFQ_MANAGE, label="Cancel"
        ),
        Transition(
            RfqStatus.SENT, RfqStatus.CLOSED, permission=Permission.RFQ_MANAGE, label="Close"
        ),
        Transition(
            RfqStatus.SENT, RfqStatus.CANCELLED, permission=Permission.RFQ_MANAGE, label="Cancel"
        ),
    ),
    terminal=frozenset({RfqStatus.CLOSED, RfqStatus.CANCELLED}),
)

QUOTATION_LIFECYCLE: StateMachine[QuotationStatus] = StateMachine(
    entity="Vendor quotation",
    initial=QuotationStatus.RECEIVED,
    transitions=(
        Transition(
            QuotationStatus.RECEIVED,
            QuotationStatus.UNDER_REVIEW,
            permission=Permission.QUOTATION_MANAGE,
            label="Start review",
        ),
        Transition(
            QuotationStatus.RECEIVED,
            QuotationStatus.SELECTED,
            permission=Permission.QUOTATION_MANAGE,
            label="Select",
        ),
        Transition(
            QuotationStatus.RECEIVED,
            QuotationStatus.REJECTED,
            permission=Permission.QUOTATION_MANAGE,
            label="Reject",
        ),
        Transition(
            QuotationStatus.RECEIVED,
            QuotationStatus.EXPIRED,
            permission=Permission.QUOTATION_MANAGE,
            label="Lapse",
        ),
        Transition(
            QuotationStatus.UNDER_REVIEW,
            QuotationStatus.SELECTED,
            permission=Permission.QUOTATION_MANAGE,
            label="Select",
        ),
        Transition(
            QuotationStatus.UNDER_REVIEW,
            QuotationStatus.REJECTED,
            permission=Permission.QUOTATION_MANAGE,
            label="Reject",
        ),
        Transition(
            QuotationStatus.UNDER_REVIEW,
            QuotationStatus.EXPIRED,
            permission=Permission.QUOTATION_MANAGE,
            label="Lapse",
        ),
    ),
    terminal=frozenset(
        {QuotationStatus.SELECTED, QuotationStatus.REJECTED, QuotationStatus.EXPIRED}
    ),
)

# The purchase order graph is database.md section 7, plus three edges it does
# not draw and one it does not need:
#
# * SENT -> PARTIALLY_RECEIVED / RECEIVED. ASSUMPTION: goods that physically
#   arrive can be received against an order the vendor never acknowledged.
#   Acknowledgement is a courtesy from the vendor, not a gate on the store; the
#   alternative is a lorry at the gate and a document that refuses to accept it.
# * RECEIVED -> GRN_COMPLETED. ASSUMPTION: an order where no line required
#   incoming inspection completes without passing through QC_PENDING. Forcing
#   every receipt through a QC state nobody inspects would make the state
#   meaningless exactly where it matters.
# * PARTIALLY_RECEIVED -> CLOSED. A short close, for the balance that will never
#   arrive. Without it a partially received order is stuck open forever, which
#   is worse than an audited decision to stop expecting the rest.
#
# CANCELLED is reachable only before any receipt, exactly as the contract says.
PURCHASE_ORDER_LIFECYCLE: StateMachine[PurchaseOrderStatus] = StateMachine(
    entity="Purchase order",
    initial=PurchaseOrderStatus.DRAFT,
    transitions=(
        Transition(
            PurchaseOrderStatus.DRAFT,
            PurchaseOrderStatus.PENDING_APPROVAL,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Submit for approval",
        ),
        Transition(
            PurchaseOrderStatus.DRAFT,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            PurchaseOrderStatus.PENDING_APPROVAL,
            PurchaseOrderStatus.APPROVED,
            permission=Permission.PURCHASE_ORDER_APPROVE,
            label="Approve",
            # Approval is the commitment: past it the order can be sent to the
            # vendor and the company is on the hook for the money.
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.PENDING_APPROVAL,
            PurchaseOrderStatus.REJECTED,
            permission=Permission.PURCHASE_ORDER_REJECT,
            label="Reject",
        ),
        Transition(
            PurchaseOrderStatus.PENDING_APPROVAL,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.SENT,
            permission=Permission.PURCHASE_ORDER_SEND,
            label="Send to vendor",
        ),
        Transition(
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.ACKNOWLEDGED,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Vendor acknowledged",
        ),
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            permission=Permission.GRN_POST,
            label="First receipt",
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.RECEIVED,
            permission=Permission.GRN_POST,
            label="Fully received",
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            PurchaseOrderStatus.ACKNOWLEDGED,
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            permission=Permission.GRN_POST,
            label="First receipt",
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.ACKNOWLEDGED,
            PurchaseOrderStatus.RECEIVED,
            permission=Permission.GRN_POST,
            label="Fully received",
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.ACKNOWLEDGED,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel",
        ),
        Transition(
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            PurchaseOrderStatus.RECEIVED,
            permission=Permission.GRN_POST,
            label="Fully received",
            is_irreversible=True,
        ),
        Transition(
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            PurchaseOrderStatus.CLOSED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Short close",
        ),
        Transition(
            PurchaseOrderStatus.RECEIVED,
            PurchaseOrderStatus.QC_PENDING,
            permission=Permission.GRN_POST,
            label="Awaiting inspection",
        ),
        Transition(
            PurchaseOrderStatus.RECEIVED,
            PurchaseOrderStatus.GRN_COMPLETED,
            permission=Permission.GRN_POST,
            label="Receipt complete",
        ),
        Transition(
            PurchaseOrderStatus.QC_PENDING,
            PurchaseOrderStatus.QC_COMPLETED,
            permission=Permission.QC_DECIDE,
            label="Inspection complete",
        ),
        Transition(
            PurchaseOrderStatus.QC_COMPLETED,
            PurchaseOrderStatus.GRN_COMPLETED,
            permission=Permission.GRN_POST,
            label="Receipt complete",
        ),
        Transition(
            PurchaseOrderStatus.GRN_COMPLETED,
            PurchaseOrderStatus.INVOICED,
            permission=Permission.VENDOR_INVOICE_CREATE,
            label="Vendor invoice booked",
        ),
        Transition(
            PurchaseOrderStatus.GRN_COMPLETED,
            PurchaseOrderStatus.CLOSED,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Close",
        ),
        Transition(
            PurchaseOrderStatus.INVOICED,
            PurchaseOrderStatus.PAID,
            permission=Permission.PAYMENT_RECORD,
            label="Paid",
        ),
        Transition(
            PurchaseOrderStatus.PAID,
            PurchaseOrderStatus.CLOSED,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Close",
        ),
    ),
    terminal=frozenset(
        {
            PurchaseOrderStatus.CLOSED,
            PurchaseOrderStatus.CANCELLED,
            PurchaseOrderStatus.REJECTED,
        }
    ),
)

GOODS_RECEIPT_LIFECYCLE: StateMachine[GoodsReceiptStatus] = StateMachine(
    entity="Goods receipt",
    initial=GoodsReceiptStatus.DRAFT,
    transitions=(
        Transition(
            GoodsReceiptStatus.DRAFT,
            GoodsReceiptStatus.POSTED,
            permission=Permission.GRN_POST,
            label="Post",
            # Posting writes the inventory ledger. There is no way back: a
            # posted receipt is reversed by a purchase return, never retracted.
            is_irreversible=True,
        ),
        Transition(
            GoodsReceiptStatus.DRAFT,
            GoodsReceiptStatus.CANCELLED,
            permission=Permission.GRN_CREATE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({GoodsReceiptStatus.POSTED, GoodsReceiptStatus.CANCELLED}),
)

PURCHASE_RETURN_LIFECYCLE: StateMachine[PurchaseReturnStatus] = StateMachine(
    entity="Purchase return",
    initial=PurchaseReturnStatus.DRAFT,
    transitions=(
        Transition(
            PurchaseReturnStatus.DRAFT,
            PurchaseReturnStatus.POSTED,
            permission=Permission.PURCHASE_RETURN_CREATE,
            label="Post",
            is_irreversible=True,
        ),
        Transition(
            PurchaseReturnStatus.DRAFT,
            PurchaseReturnStatus.CANCELLED,
            permission=Permission.PURCHASE_RETURN_CREATE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({PurchaseReturnStatus.POSTED, PurchaseReturnStatus.CANCELLED}),
)


# =============================================================================
# Line and document arithmetic
# =============================================================================


@dataclass(frozen=True, slots=True)
class LineAmounts:
    """Everything money-shaped about one line, all at money scale."""

    gross_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    tax_amount: Decimal
    line_total: Decimal


def compute_line_amounts(
    *,
    quantity: Decimal,
    unit_price: Decimal,
    tax_rate: Decimal,
    discount_percentage: Decimal | None = None,
    discount_amount: Decimal | None = None,
) -> LineAmounts:
    """Price one purchase line: gross, discount, taxable, tax, total.

    Rounding is applied at each stored value rather than once at the end,
    because every one of these numbers is written to a ``NUMERIC`` column and
    reconciled against the vendor's invoice. A total that cannot be reproduced
    from the columns beside it is a three-way-match failure waiting to happen.

    Purchase tax is a single amount, not a CGST/SGST/IGST split: the vendor's
    invoice carries the statutory split, and this document records what was
    agreed. Overstating the split here would put this module's guess on a
    document finance later has to match against the real one.
    """
    if quantity <= 0:
        raise ValidationError("Line quantity must be greater than zero.")
    if unit_price < 0:
        raise ValidationError("Line unit price cannot be negative.")
    if tax_rate < 0:
        raise ValidationError("Tax rate cannot be negative.")

    gross = to_money(Decimal(quantity) * Decimal(unit_price))

    if discount_percentage is not None and discount_percentage > 0:
        discount = to_money(gross * Decimal(discount_percentage) / Decimal(100))
    elif discount_amount is not None:
        discount = to_money(discount_amount)
    else:
        discount = to_money(ZERO)

    if discount > gross:
        raise ValidationError(
            "Line discount is larger than the line value.",
            details={"gross_amount": str(gross), "discount_amount": str(discount)},
        )

    taxable = gross - discount
    tax = to_money(taxable * Decimal(tax_rate) / Decimal(100))

    return LineAmounts(
        gross_amount=gross,
        discount_amount=discount,
        taxable_amount=taxable,
        tax_amount=tax,
        line_total=taxable + tax,
    )


@dataclass(frozen=True, slots=True)
class DocumentTotals:
    subtotal: Decimal
    discount: Decimal
    tax_amount: Decimal
    freight_amount: Decimal
    total_amount: Decimal


def sum_document_totals(
    amounts: list[LineAmounts], *, freight_amount: Decimal | None = None
) -> DocumentTotals:
    """Roll line amounts up to the header.

    ``subtotal`` is the gross value before discount, so the header reads
    ``subtotal - discount + tax + freight = total`` - the arithmetic a buyer
    checks by hand against the vendor's proforma.
    """
    subtotal = to_money(sum((a.gross_amount for a in amounts), ZERO))
    discount = to_money(sum((a.discount_amount for a in amounts), ZERO))
    tax = to_money(sum((a.tax_amount for a in amounts), ZERO))
    freight = to_money(freight_amount if freight_amount is not None else ZERO)
    return DocumentTotals(
        subtotal=subtotal,
        discount=discount,
        tax_amount=tax,
        freight_amount=freight,
        total_amount=subtotal - discount + tax + freight,
    )


def effective_unit_price(
    *, unit_price: Decimal, discount_percentage: Decimal | None = None
) -> Decimal:
    """Unit price after a percentage discount.

    The only number two vendors' offers can honestly be compared on: list price
    minus a discount structure is what will actually be paid per unit.
    """
    if discount_percentage is None or discount_percentage <= 0:
        return Decimal(unit_price)
    factor = (Decimal(100) - Decimal(discount_percentage)) / Decimal(100)
    return (Decimal(unit_price) * factor).quantize(Decimal("0.000001"))


# =============================================================================
# Receiving: the over-receipt tolerance (master rules section 17)
# =============================================================================


def max_receivable_quantity(ordered_quantity: Decimal) -> Decimal:
    """The most that may ever be received against a line, tolerance included."""
    return to_quantity(Decimal(ordered_quantity) * OVER_RECEIPT_MULTIPLIER)


def remaining_receivable(ordered_quantity: Decimal, already_received: Decimal) -> Decimal:
    """How much more this line will still accept. Never negative."""
    remaining = max_receivable_quantity(ordered_quantity) - to_quantity(Decimal(already_received))
    return remaining if remaining > 0 else to_quantity(ZERO)


def assert_within_over_receipt_tolerance(
    *,
    item_code: str,
    line_number: int,
    ordered_quantity: Decimal,
    already_received: Decimal,
    incoming_quantity: Decimal,
) -> None:
    """Refuse a delivery that would push a line past the tolerance.

    The message names the numbers rather than the rule, because the storekeeper
    reading it has a lorry at the gate and needs to know what to accept, not
    which constant was breached.
    """
    proposed = to_quantity(Decimal(already_received) + Decimal(incoming_quantity))
    ceiling = max_receivable_quantity(ordered_quantity)
    if proposed <= ceiling:
        return

    raise BusinessRuleError(
        f"Line {line_number} ({item_code}): receiving {to_quantity(Decimal(incoming_quantity))} "
        f"would bring the total received to {proposed}, above the {ceiling} allowed for an "
        f"order of {to_quantity(Decimal(ordered_quantity))} "
        f"({OVER_RECEIPT_TOLERANCE * 100:.0f}% over-receipt tolerance). "
        f"Accept at most {remaining_receivable(ordered_quantity, already_received)} "
        "or amend the purchase order.",
        details={
            "line_number": line_number,
            "item_code": item_code,
            "ordered_quantity": str(to_quantity(Decimal(ordered_quantity))),
            "already_received": str(to_quantity(Decimal(already_received))),
            "incoming_quantity": str(to_quantity(Decimal(incoming_quantity))),
            "maximum_receivable": str(ceiling),
            "remaining_receivable": str(
                remaining_receivable(ordered_quantity, already_received)
            ),
            "tolerance_percentage": str(OVER_RECEIPT_TOLERANCE * 100),
        },
    )


def po_line_status_for(
    *,
    quantity: Decimal,
    received_quantity: Decimal,
    cancelled: bool = False,
    short_closed: bool = False,
) -> PurchaseOrderLineStatus:
    """Derive a line's status from its quantities.

    Derived rather than assigned so a line can never claim RECEIVED while its
    received quantity is short - the status and the numbers are one fact, and
    two writers of the same fact eventually disagree.
    """
    if cancelled:
        return PurchaseOrderLineStatus.CANCELLED
    if short_closed:
        return PurchaseOrderLineStatus.CLOSED
    if Decimal(received_quantity) >= Decimal(quantity):
        return PurchaseOrderLineStatus.RECEIVED
    if Decimal(received_quantity) > 0:
        return PurchaseOrderLineStatus.PARTIALLY_RECEIVED
    return PurchaseOrderLineStatus.PENDING


def requisition_line_status_for(
    *, quantity: Decimal, ordered_quantity: Decimal, cancelled: bool = False
) -> RequisitionLineStatus:
    if cancelled:
        return RequisitionLineStatus.CANCELLED
    if Decimal(ordered_quantity) >= Decimal(quantity):
        return RequisitionLineStatus.ORDERED
    if Decimal(ordered_quantity) > 0:
        return RequisitionLineStatus.PARTIALLY_ORDERED
    return RequisitionLineStatus.PENDING


class LineReceiptState(Protocol):
    """The two numbers order-level status resolution needs from each line."""

    @property
    def quantity(self) -> Decimal: ...

    @property
    def received_quantity(self) -> Decimal: ...


def order_is_fully_received(lines: list[tuple[Decimal, Decimal, bool]]) -> bool:
    """Whether every live line has had its full quantity delivered.

    ``lines`` is ``(ordered, received, is_closed)``. A short-closed or cancelled
    line does not hold the order open - that is the whole point of closing it.
    """
    live = [(ordered, received) for ordered, received, closed in lines if not closed]
    if not live:
        return True
    return all(Decimal(received) >= Decimal(ordered) for ordered, received in live)


def receipt_unit_cost(
    *, quantity: Decimal, taxable_amount: Decimal, fallback_unit_price: Decimal
) -> Decimal:
    """Inventory valuation for a received unit.

    The net price after discount and *before* tax: GST on a purchase is an input
    credit, not part of what the material cost the company.

    ASSUMPTION: header freight is not apportioned into the item cost at receipt.
    Apportioning it needs a basis (value, weight, volume) that nobody has agreed
    yet, and a wrong basis silently misstates the cost of every item on the
    order. Freight stays a header cost for finance to treat as landed cost when
    the basis is decided.
    """
    if quantity <= 0:
        return Decimal(fallback_unit_price).quantize(Decimal("0.000001"))
    return (Decimal(taxable_amount) / Decimal(quantity)).quantize(Decimal("0.000001"))


# =============================================================================
# Duplicate purchase order prevention (master rules section 16)
# =============================================================================


class DuplicateAction(StrEnum):
    """What a buyer may do about a duplicate, from master rules section 16."""

    MERGE_WITH_EXISTING = "MERGE_WITH_EXISTING"
    INCREASE_EXISTING_QUANTITY = "INCREASE_EXISTING_QUANTITY"
    CREATE_NEW_WITH_APPROVAL = "CREATE_NEW_WITH_APPROVAL"
    CANCEL_DUPLICATE = "CANCEL_DUPLICATE"


class DuplicateConfidence(StrEnum):
    """How sure the matcher is, which drives how loudly the UI should object."""

    #: The two orders name the same source demand. This is not a coincidence.
    EXACT = "EXACT"
    #: Same vendor, item, warehouse and date window, and the existing order has
    #: not reached the vendor yet - so it is still cheap to fix.
    STRONG = "STRONG"
    POSSIBLE = "POSSIBLE"


#: Statuses in which the existing order has not yet been sent to the vendor, so
#: absorbing the new demand into it costs nothing outside the building.
_NOT_YET_WITH_VENDOR: frozenset[PurchaseOrderStatus] = frozenset(
    {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
    }
)

#: Statuses in which lines can still be edited into the existing document.
_MERGEABLE: frozenset[PurchaseOrderStatus] = frozenset(
    {PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL}
)

#: Statuses from which the existing order can still be cancelled outright.
_CANCELLABLE: frozenset[PurchaseOrderStatus] = frozenset(
    {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.ACKNOWLEDGED,
    }
)


@dataclass(frozen=True, slots=True)
class DuplicateCandidate:
    """An existing open order line that may be covering the same demand."""

    purchase_order_id: uuid.UUID
    purchase_order_code: str
    purchase_order_line_id: uuid.UUID
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    ordered_quantity: Decimal
    received_quantity: Decimal
    required_date: date | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None
    confidence: DuplicateConfidence
    match_reasons: tuple[str, ...]
    recommended_action: DuplicateAction
    available_actions: tuple[DuplicateAction, ...]

    @property
    def open_quantity(self) -> Decimal:
        remaining = Decimal(self.ordered_quantity) - Decimal(self.received_quantity)
        return to_quantity(remaining if remaining > 0 else ZERO)


def dates_overlap(
    candidate: date | None,
    proposed: date | None,
    *,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
) -> bool:
    """Whether two required dates fall inside the same demand window.

    ASSUMPTION: an unknown date on either side counts as overlapping. A missing
    date is no evidence that the two orders are for different needs, and the
    expensive mistake here is the false negative - the duplicate that is never
    shown to anyone.
    """
    if candidate is None or proposed is None:
        return True
    return abs((candidate - proposed).days) <= window_days


def same_source_demand(
    *,
    candidate_source_type: PurchaseSourceType,
    candidate_source_id: uuid.UUID | None,
    proposed_source_type: PurchaseSourceType | None,
    proposed_source_id: uuid.UUID | None,
) -> bool:
    """Whether both orders were raised to cover the same recorded demand.

    ``MANUAL`` is never a shared source: two hand-typed orders have no common
    reference, and treating the absence of one as a match would flag every
    manual order against every other.
    """
    if proposed_source_id is None or candidate_source_id is None:
        return False
    if proposed_source_type is None or candidate_source_type is PurchaseSourceType.MANUAL:
        return False
    return (
        candidate_source_type == proposed_source_type and candidate_source_id == proposed_source_id
    )


def available_actions_for(
    *, status: PurchaseOrderStatus, received_quantity: Decimal
) -> tuple[DuplicateAction, ...]:
    """Which of section 16's four actions are actually possible here.

    A recommendation the system cannot carry out is worse than none: the buyer
    clicks it, it fails, and next time they ignore the warning entirely.
    """
    nothing_received = Decimal(received_quantity) <= 0
    actions: list[DuplicateAction] = []

    if status in _MERGEABLE:
        actions.append(DuplicateAction.MERGE_WITH_EXISTING)
    if nothing_received and status in _CANCELLABLE:
        actions.append(DuplicateAction.INCREASE_EXISTING_QUANTITY)
        actions.append(DuplicateAction.CANCEL_DUPLICATE)
    # Always available: the buyer may know something the matcher does not, and
    # the approval requirement is what makes that safe.
    actions.append(DuplicateAction.CREATE_NEW_WITH_APPROVAL)
    return tuple(actions)


def recommend_action(
    *,
    status: PurchaseOrderStatus,
    received_quantity: Decimal,
    is_same_source: bool,
    available: tuple[DuplicateAction, ...],
) -> DuplicateAction:
    """Pick the action to put in front of the buyer.

    The ladder, strongest signal first:

    1. Same source demand and the existing order can still be stopped - the new
       order would double-buy a shortage that is already covered, so cancelling
       one of them is the honest answer.
    2. The existing order has not left the building - merge into it.
    3. Nothing has been received yet - amend its quantity rather than opening a
       second order the vendor has to reconcile.
    4. Otherwise the demand is genuinely additional; raise a new order, but with
       somebody's approval on it.
    """
    if is_same_source and DuplicateAction.CANCEL_DUPLICATE in available:
        return DuplicateAction.CANCEL_DUPLICATE
    if DuplicateAction.MERGE_WITH_EXISTING in available:
        return DuplicateAction.MERGE_WITH_EXISTING
    if DuplicateAction.INCREASE_EXISTING_QUANTITY in available:
        return DuplicateAction.INCREASE_EXISTING_QUANTITY
    return DuplicateAction.CREATE_NEW_WITH_APPROVAL


@dataclass(frozen=True, slots=True)
class ProposedLine:
    """The demand being checked, as the matcher needs to see it."""

    item_id: uuid.UUID
    required_date: date | None


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """An open order line, decoupled from the repository row that produced it.

    Its own type so the matcher can be exercised with hand-built cases rather
    than a database fixture.
    """

    purchase_order_id: uuid.UUID
    purchase_order_code: str
    purchase_order_line_id: uuid.UUID
    status: PurchaseOrderStatus
    vendor_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    ordered_quantity: Decimal
    received_quantity: Decimal
    required_date: date | None
    source_type: PurchaseSourceType
    source_id: uuid.UUID | None


def classify_candidate(
    candidate: CandidateInput,
    *,
    vendor_id: uuid.UUID,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    required_date: date | None,
    source_type: PurchaseSourceType | None,
    source_id: uuid.UUID | None,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
) -> DuplicateCandidate | None:
    """Decide whether one open line is a duplicate of the proposed demand.

    Returns ``None`` when it is not. The four hard criteria from master rules
    section 16 - same vendor, same item, same warehouse, overlapping required
    date - all have to hold, except that naming the same source demand
    overrides the date window: two orders raised from one MRP suggestion are
    duplicates whatever dates were typed on them.
    """
    if candidate.vendor_id != vendor_id:
        return None
    if candidate.item_id != item_id:
        return None
    if candidate.warehouse_id != warehouse_id:
        return None

    is_same_source = same_source_demand(
        candidate_source_type=candidate.source_type,
        candidate_source_id=candidate.source_id,
        proposed_source_type=source_type,
        proposed_source_id=source_id,
    )
    in_window = dates_overlap(candidate.required_date, required_date, window_days=window_days)
    if not in_window and not is_same_source:
        return None

    reasons = ["Same vendor", "Same item", "Same delivery warehouse"]
    if in_window:
        reasons.append(
            "Required dates within "
            f"{window_days} day(s)"
            if candidate.required_date and required_date
            else "One of the two orders has no required date"
        )
    if is_same_source:
        reasons.append(f"Both raised from the same {candidate.source_type.value} demand")
    if Decimal(candidate.received_quantity) <= 0:
        reasons.append("Nothing received against the existing order yet")

    if is_same_source:
        confidence = DuplicateConfidence.EXACT
    elif candidate.status in _NOT_YET_WITH_VENDOR:
        confidence = DuplicateConfidence.STRONG
    else:
        confidence = DuplicateConfidence.POSSIBLE

    available = available_actions_for(
        status=candidate.status, received_quantity=candidate.received_quantity
    )
    recommended = recommend_action(
        status=candidate.status,
        received_quantity=candidate.received_quantity,
        is_same_source=is_same_source,
        available=available,
    )

    return DuplicateCandidate(
        purchase_order_id=candidate.purchase_order_id,
        purchase_order_code=candidate.purchase_order_code,
        purchase_order_line_id=candidate.purchase_order_line_id,
        status=candidate.status,
        vendor_id=candidate.vendor_id,
        item_id=candidate.item_id,
        warehouse_id=candidate.warehouse_id,
        ordered_quantity=to_quantity(Decimal(candidate.ordered_quantity)),
        received_quantity=to_quantity(Decimal(candidate.received_quantity)),
        required_date=candidate.required_date,
        source_type=candidate.source_type,
        source_id=candidate.source_id,
        confidence=confidence,
        match_reasons=tuple(reasons),
        recommended_action=recommended,
        available_actions=available,
    )


#: Strongest first, so a caller showing one candidate shows the worst case.
_CONFIDENCE_ORDER = {
    DuplicateConfidence.EXACT: 0,
    DuplicateConfidence.STRONG: 1,
    DuplicateConfidence.POSSIBLE: 2,
}


def classify_candidates(
    candidates: list[CandidateInput],
    *,
    vendor_id: uuid.UUID,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    required_date: date | None,
    source_type: PurchaseSourceType | None,
    source_id: uuid.UUID | None,
    window_days: int = DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
) -> list[DuplicateCandidate]:
    """Classify a set of open lines, strongest match first."""
    matched = [
        classified
        for candidate in candidates
        if (
            classified := classify_candidate(
                candidate,
                vendor_id=vendor_id,
                item_id=item_id,
                warehouse_id=warehouse_id,
                required_date=required_date,
                source_type=source_type,
                source_id=source_id,
                window_days=window_days,
            )
        )
        is not None
    ]
    return sorted(
        matched,
        key=lambda c: (
            _CONFIDENCE_ORDER[c.confidence],
            c.required_date or date.max,
            str(c.purchase_order_code),
        ),
    )


def duplicate_refusal(candidates: list[DuplicateCandidate]) -> BusinessRuleError:
    """The error raised when an order with duplicates is created without
    acknowledgement.

    Carries every candidate and its possible actions, because a refusal that
    only says "duplicate" leaves the buyer with nothing to do but retry.
    """
    strongest = candidates[0]
    return BusinessRuleError(
        f"This order duplicates {len(candidates)} open purchase order line(s) - "
        f"{strongest.purchase_order_code} already covers the same item for the same vendor, "
        "warehouse and delivery window. Review the suggested action, or resubmit with "
        "acknowledge_duplicates and a reason if the demand is genuinely additional.",
        code=ErrorCode.DUPLICATE_DOCUMENT,
        status_code=409,
        details={
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "purchase_order_id": str(c.purchase_order_id),
                    "purchase_order_code": c.purchase_order_code,
                    "purchase_order_line_id": str(c.purchase_order_line_id),
                    "item_id": str(c.item_id),
                    "status": str(c.status),
                    "open_quantity": str(c.open_quantity),
                    "required_date": c.required_date.isoformat() if c.required_date else None,
                    "confidence": str(c.confidence),
                    "match_reasons": list(c.match_reasons),
                    "recommended_action": str(c.recommended_action),
                    "available_actions": [str(a) for a in c.available_actions],
                }
                for c in candidates
            ],
        },
    )


# =============================================================================
# Quotation comparison and recommendation (master rules section 15)
# =============================================================================


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """How much each factor counts towards the recommendation."""

    price: Decimal
    lead_time: Decimal
    rating: Decimal

    def total(self) -> Decimal:
        return self.price + self.lead_time + self.rating


# ASSUMPTION: price carries 60% of the recommendation, lead time and vendor
# rating 20% each. Price is what the business is buying on, but a part that
# arrives four weeks late stops a production line, and a vendor with a poor
# quality record costs more than the discount they offered. The weights are one
# constant to change - and the response always states the reason, so a buyer can
# see when the recommendation disagrees with the cheapest price and why.
DEFAULT_SCORE_WEIGHTS = ScoreWeights(
    price=Decimal("0.60"), lead_time=Decimal("0.20"), rating=Decimal("0.20")
)

# ASSUMPTION: an unrated vendor scores neutrally rather than badly. A vendor with
# no delivery history is unknown, not poor (the vendors module makes the same
# distinction by leaving ``rating`` NULL), and scoring them zero would make it
# impossible for a new vendor ever to win their first order.
UNRATED_VENDOR_SCORE = Decimal("0.5")

_SCORE_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class OfferInput:
    """One vendor's offer for one item, as the ranker needs it."""

    vendor_id: uuid.UUID
    quotation_id: uuid.UUID
    quotation_line_id: uuid.UUID
    effective_unit_price: Decimal
    lead_time_days: int
    vendor_rating: Decimal | None
    is_expired: bool = False


@dataclass(frozen=True, slots=True)
class RankedOffer:
    offer: OfferInput
    score: Decimal
    rank: int
    is_lowest_price: bool
    is_fastest: bool


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("1")
    return (numerator / denominator).quantize(_SCORE_QUANTUM)


def score_offer(
    offer: OfferInput,
    *,
    best_price: Decimal,
    best_lead_time: int,
    weights: ScoreWeights = DEFAULT_SCORE_WEIGHTS,
) -> Decimal:
    """Blend price, lead time and rating into one 0-1 score.

    Each factor is expressed as "how close is this to the best offer on the
    table", so the score is comparable across RFQs of wildly different values.
    Lead time is compared on ``best + 1`` over ``mine + 1`` so a zero-day
    (ex-stock) quote does not divide by zero and does not make every other offer
    score zero.
    """
    price_score = (
        Decimal("1")
        if Decimal(offer.effective_unit_price) <= 0
        else _ratio(Decimal(best_price), Decimal(offer.effective_unit_price))
    )
    lead_score = _ratio(Decimal(best_lead_time + 1), Decimal(offer.lead_time_days + 1))
    rating_score = (
        UNRATED_VENDOR_SCORE
        if offer.vendor_rating is None
        else _ratio(Decimal(offer.vendor_rating), Decimal(MAX_VENDOR_RATING))
    )

    weighted = (
        weights.price * price_score
        + weights.lead_time * lead_score
        + weights.rating * rating_score
    )
    divisor = weights.total()
    if divisor <= 0:
        raise ValidationError("Quotation score weights must sum to more than zero.")
    return (weighted / divisor).quantize(_SCORE_QUANTUM)


def rank_offers(
    offers: list[OfferInput], *, weights: ScoreWeights = DEFAULT_SCORE_WEIGHTS
) -> list[RankedOffer]:
    """Score and rank every offer for one item, best first.

    An expired quotation is ranked last with a zero score rather than dropped:
    the buyer needs to see that the cheapest price on the table has lapsed, not
    to wonder why a vendor they remember quoting has vanished from the table.
    """
    if not offers:
        return []

    live = [o for o in offers if not o.is_expired]
    if live:
        best_price = min(Decimal(o.effective_unit_price) for o in live)
        best_lead_time = min(o.lead_time_days for o in live)
        lowest_price_ids = {
            o.quotation_line_id
            for o in live
            if Decimal(o.effective_unit_price) == best_price
        }
        fastest_ids = {o.quotation_line_id for o in live if o.lead_time_days == best_lead_time}
    else:
        best_price, best_lead_time = Decimal("0"), 0
        lowest_price_ids, fastest_ids = set(), set()

    scored = [
        (
            offer,
            Decimal("0")
            if offer.is_expired
            else score_offer(
                offer, best_price=best_price, best_lead_time=best_lead_time, weights=weights
            ),
        )
        for offer in offers
    ]
    # Deterministic ordering: score, then price, then id. Without the last two
    # a tie would rank differently between two identical requests.
    scored.sort(
        key=lambda pair: (
            -pair[1],
            Decimal(pair[0].effective_unit_price),
            str(pair[0].quotation_line_id),
        )
    )

    return [
        RankedOffer(
            offer=offer,
            score=score,
            rank=position,
            is_lowest_price=offer.quotation_line_id in lowest_price_ids,
            is_fastest=offer.quotation_line_id in fastest_ids,
        )
        for position, (offer, score) in enumerate(scored, start=1)
    ]


def recommendation_reason(ranked: list[RankedOffer], *, vendor_names: dict[uuid.UUID, str]) -> str:
    """State, in one sentence, why the winner won.

    Generated from the facts that decided it rather than written by hand, so the
    reason cannot drift away from the score it is explaining - and so a buyer
    overriding it is arguing with the actual criteria.
    """
    if not ranked:
        return "No quotations have been received for this item."

    live = [r for r in ranked if not r.offer.is_expired]
    if not live:
        return "Every quotation for this item has lapsed; ask the vendors to requote."

    best = live[0]
    name = vendor_names.get(best.offer.vendor_id, "the leading vendor")
    parts: list[str] = []

    if best.is_lowest_price:
        parts.append(f"lowest effective unit price ({best.offer.effective_unit_price})")
    else:
        cheapest = min(live, key=lambda r: Decimal(r.offer.effective_unit_price))
        premium = Decimal(best.offer.effective_unit_price) - Decimal(
            cheapest.offer.effective_unit_price
        )
        cheapest_name = vendor_names.get(cheapest.offer.vendor_id, "the cheapest offer")
        parts.append(
            f"{premium} per unit above {cheapest_name}, outweighed by delivery and quality"
        )

    parts.append(
        f"{best.offer.lead_time_days}-day lead time"
        + (" (fastest offered)" if best.is_fastest else "")
    )
    parts.append(
        "no delivery history yet"
        if best.offer.vendor_rating is None
        else f"vendor rating {best.offer.vendor_rating} of {MAX_VENDOR_RATING}"
    )

    if len(live) == 1:
        return f"{name} is the only live quotation: {', '.join(parts)}."
    return (
        f"{name} scores {best.score} against {len(live)} live quotations: "
        f"{', '.join(parts)}."
    )


__all__ = [
    "DEFAULT_SCORE_WEIGHTS",
    "GOODS_RECEIPT_LIFECYCLE",
    "PURCHASE_ORDER_LIFECYCLE",
    "PURCHASE_RETURN_LIFECYCLE",
    "QUOTATION_LIFECYCLE",
    "REQUISITION_LIFECYCLE",
    "RFQ_LIFECYCLE",
    "UNRATED_VENDOR_SCORE",
    "CandidateInput",
    "DocumentTotals",
    "DuplicateAction",
    "DuplicateCandidate",
    "DuplicateConfidence",
    "LineAmounts",
    "LineReceiptState",
    "OfferInput",
    "ProposedLine",
    "RankedOffer",
    "ScoreWeights",
    "assert_within_over_receipt_tolerance",
    "available_actions_for",
    "classify_candidate",
    "classify_candidates",
    "compute_line_amounts",
    "dates_overlap",
    "duplicate_refusal",
    "effective_unit_price",
    "max_receivable_quantity",
    "order_is_fully_received",
    "po_line_status_for",
    "rank_offers",
    "receipt_unit_cost",
    "recommend_action",
    "recommendation_reason",
    "remaining_receivable",
    "requisition_line_status_for",
    "same_source_demand",
    "score_offer",
    "sum_document_totals",
]
