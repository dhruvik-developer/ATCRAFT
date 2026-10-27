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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from src.core.demand_provenance import DemandReference
from src.core.errors import BusinessRuleError, ErrorCode, ValidationError
from src.core.permissions import Permission
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, Money, to_money, to_quantity
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
    ReceiptLineQcStatus,
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
        Transition(RfqStatus.DRAFT, RfqStatus.SENT, permission=Permission.RFQ_MANAGE, label="Send"),
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
        # ASSUMPTION: rejecting or lapsing an offer is not final while the RFQ
        # is still open. The comparison tells the buyer to "ask the vendors to
        # requote", and the one row per (RFQ, vendor) the unique constraint
        # allows is reopened for the revised offer rather than a second row
        # being inserted. Only a SELECTED offer - the one a purchase order is
        # raised against - stays final. Recorded in docs/assumptions.md.
        Transition(
            QuotationStatus.REJECTED,
            QuotationStatus.RECEIVED,
            permission=Permission.QUOTATION_MANAGE,
            label="Requote",
        ),
        Transition(
            QuotationStatus.EXPIRED,
            QuotationStatus.RECEIVED,
            permission=Permission.QUOTATION_MANAGE,
            label="Requote",
        ),
    ),
    terminal=frozenset({QuotationStatus.SELECTED}),
)

#: Statuses a vendor may revise its way out of by requoting. Read by
#: ``service.update_quotation`` so the reopen path and the state machine agree.
REQUOTABLE_QUOTATION_STATUSES: frozenset[QuotationStatus] = frozenset(
    {QuotationStatus.REJECTED, QuotationStatus.EXPIRED}
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
        # These edges belong to the amendment service: it preserves line IDs,
        # checks all GRN history and records a new revision before reopening
        # approval. Ordinary submission remains limited to DRAFT.
        Transition(
            PurchaseOrderStatus.APPROVED,
            PurchaseOrderStatus.PENDING_APPROVAL,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Amend",
        ),
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.PENDING_APPROVAL,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Amend",
        ),
        Transition(
            PurchaseOrderStatus.ACKNOWLEDGED,
            PurchaseOrderStatus.PENDING_APPROVAL,
            permission=Permission.PURCHASE_ORDER_UPDATE,
            label="Amend",
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
        Transition(
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.VENDOR_DECLINED,
            permission=Permission.VENDOR_PORTAL_RESPOND,
            label="Vendor declined",
        ),
        Transition(
            PurchaseOrderStatus.VENDOR_DECLINED,
            PurchaseOrderStatus.CANCELLED,
            permission=Permission.PURCHASE_ORDER_CANCEL,
            label="Cancel declined order",
        ),
        Transition(
            PurchaseOrderStatus.VENDOR_DECLINED,
            PurchaseOrderStatus.SENT,
            permission=Permission.PURCHASE_ORDER_SEND,
            label="Resend declined order",
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


def _stored_money(value: Decimal | int | str) -> Decimal:
    """Reject derived overflow before rounding or writing a document.

    Individually valid quantities and prices can multiply/sum beyond the
    shared Money column. That is an input refusal, not a database outage.
    """
    value = Decimal(value)
    precision, scale = Money.precision, Money.scale
    assert precision is not None and scale is not None
    bound = Decimal(10) ** (precision - scale)
    if not value.is_finite() or abs(value) >= bound:
        raise ValidationError("The calculated amount is too large. Reduce the quantity or price.")
    rounded = to_money(value)
    if abs(rounded) >= bound:
        raise ValidationError("The calculated amount is too large. Reduce the quantity or price.")
    return rounded


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
    if any(
        value is not None and not Decimal(value).is_finite()
        for value in (quantity, unit_price, tax_rate, discount_percentage, discount_amount)
    ):
        raise ValidationError("Amounts and quantities must be finite decimal values.")
    if quantity <= 0:
        raise ValidationError("Line quantity must be greater than zero.")
    if unit_price < 0:
        raise ValidationError("Line unit price cannot be negative.")
    if tax_rate < 0:
        raise ValidationError("Tax rate cannot be negative.")

    gross = _stored_money(Decimal(quantity) * Decimal(unit_price))

    if discount_percentage is not None and discount_percentage > 0:
        discount = _stored_money(gross * Decimal(discount_percentage) / Decimal(100))
    elif discount_amount is not None:
        discount = _stored_money(discount_amount)
    else:
        discount = _stored_money(ZERO)

    if discount > gross:
        raise ValidationError(
            "Line discount is larger than the line value.",
            details={"gross_amount": str(gross), "discount_amount": str(discount)},
        )

    taxable = gross - discount
    tax = _stored_money(taxable * Decimal(tax_rate) / Decimal(100))

    return LineAmounts(
        gross_amount=gross,
        discount_amount=discount,
        taxable_amount=taxable,
        tax_amount=tax,
        line_total=_stored_money(taxable + tax),
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
    subtotal = _stored_money(sum((a.gross_amount for a in amounts), ZERO))
    discount = _stored_money(sum((a.discount_amount for a in amounts), ZERO))
    tax = _stored_money(sum((a.tax_amount for a in amounts), ZERO))
    freight = _stored_money(freight_amount if freight_amount is not None else ZERO)
    return DocumentTotals(
        subtotal=subtotal,
        discount=discount,
        tax_amount=tax,
        freight_amount=freight,
        total_amount=_stored_money(subtotal - discount + tax + freight),
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
            "remaining_receivable": str(remaining_receivable(ordered_quantity, already_received)),
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

#: Direct edits in the first two states; an approved or vendor-visible order
#: can absorb demand only through the audited amendment service.
_MERGEABLE: frozenset[PurchaseOrderStatus] = frozenset(
    {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.ACKNOWLEDGED,
    }
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
    item_category_id: uuid.UUID | None = None
    has_receipt_history: bool = False
    purchase_order_version: int = 1

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
    *,
    status: PurchaseOrderStatus,
    received_quantity: Decimal,
    has_receipt_history: bool = False,
    same_item: bool = True,
) -> tuple[DuplicateAction, ...]:
    """Which of section 16's four actions are actually possible here.

    A recommendation the system cannot carry out is worse than none: the buyer
    clicks it, it fails, and next time they ignore the warning entirely.
    """
    nothing_received = not has_receipt_history and Decimal(received_quantity) <= 0
    actions: list[DuplicateAction] = []

    if nothing_received and status in _MERGEABLE:
        actions.append(DuplicateAction.MERGE_WITH_EXISTING)
    if nothing_received and status in _CANCELLABLE:
        if same_item:
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
    if (
        status in (PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.PENDING_APPROVAL)
        and DuplicateAction.MERGE_WITH_EXISTING in available
    ):
        return DuplicateAction.MERGE_WITH_EXISTING
    if DuplicateAction.INCREASE_EXISTING_QUANTITY in available:
        return DuplicateAction.INCREASE_EXISTING_QUANTITY
    if DuplicateAction.MERGE_WITH_EXISTING in available:
        return DuplicateAction.MERGE_WITH_EXISTING
    return DuplicateAction.CREATE_NEW_WITH_APPROVAL


@dataclass(frozen=True, slots=True)
class ProposedLine:
    """The demand being checked, as the matcher needs to see it.

    ``requisition_line_id`` is the pending requisition line the proposed order
    line would satisfy, when there is one - the "same pending PR" signal.
    """

    item_id: uuid.UUID
    required_date: date | None
    requisition_line_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None
    source_demand_refs: tuple[Mapping[str, object] | DemandReference, ...] = ()
    requisition_line_ids: frozenset[uuid.UUID] = frozenset()


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
    #: The requisition line the existing order line was raised to satisfy.
    requisition_line_id: uuid.UUID | None = None
    item_category_id: uuid.UUID | None = None
    source_demand_refs: tuple[Mapping[str, object] | DemandReference, ...] = ()
    requisition_line_ids: frozenset[uuid.UUID] = frozenset()
    has_receipt_history: bool = False
    purchase_order_version: int = 1


def demand_ref_keys(
    refs: Sequence[Mapping[str, object] | DemandReference],
) -> frozenset[tuple[str, uuid.UUID, uuid.UUID | None]]:
    """Canonical triples in a validated PR list, without MRP display inference.

    Historical malformed entries carry no trustworthy identity. Ignore them
    instead of comparing missing keys (which would make unrelated rows EXACT).
    Tenant ownership is validated by the writing service, outside this pure
    matcher. A complete PR list of fifty entries is valid here.
    """
    keys: set[tuple[str, uuid.UUID, uuid.UUID | None]] = set()
    for raw in refs:
        try:
            ref = DemandReference.model_validate(raw)
        except (TypeError, ValueError):
            continue
        keys.add((ref.demand_type, ref.demand_id, ref.demand_line_id))
    return frozenset(keys)


def same_requisition_line(
    *,
    candidate_requisition_line_id: uuid.UUID | None,
    proposed_requisition_line_id: uuid.UUID | None,
) -> bool:
    """Whether both order lines claim the same pending requisition line.

    The line-level form of :func:`same_source_demand`. A purchase order raised
    from several requisitions carries no single header source, so the header
    pair alone could never see that an open order already covers this exact
    line of this exact requisition - which is the "same pending PR" rule of
    master rules section 16.
    """
    return (
        proposed_requisition_line_id is not None
        and candidate_requisition_line_id == proposed_requisition_line_id
    )


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
    requisition_line_id: uuid.UUID | None = None,
    item_category_id: uuid.UUID | None = None,
    source_demand_refs: Sequence[Mapping[str, object] | DemandReference] = (),
    requisition_line_ids: Collection[uuid.UUID] = (),
) -> DuplicateCandidate | None:
    """Decide whether one open line is a duplicate of the proposed demand.

    Matching source identities for the same item override the date window.
    A different item in the same procurement category is a weaker, dated
    POSSIBLE signal; it can be merged but cannot increase another item's line.
    """
    if candidate.vendor_id != vendor_id:
        return None
    if candidate.warehouse_id != warehouse_id:
        return None
    same_item = candidate.item_id == item_id
    same_category = item_category_id is not None and candidate.item_category_id == item_category_id
    in_window = dates_overlap(candidate.required_date, required_date, window_days=window_days)
    if not same_item and not (same_category and in_window):
        return None

    is_same_source = same_item and same_source_demand(
        candidate_source_type=candidate.source_type,
        candidate_source_id=candidate.source_id,
        proposed_source_type=source_type,
        proposed_source_id=source_id,
    )
    candidate_requisitions = candidate.requisition_line_ids | (
        frozenset({candidate.requisition_line_id}) if candidate.requisition_line_id else frozenset()
    )
    proposed_requisitions = set(requisition_line_ids)
    if requisition_line_id is not None:
        proposed_requisitions.add(requisition_line_id)
    is_same_requisition_line = same_item and bool(candidate_requisitions & proposed_requisitions)
    # ASSUMPTION: a common sales/BOM root can require several different
    # components. Root overlap is EXACT only for the same SKU, while the
    # separate category signal still warns about related purchases.
    is_same_root = same_item and bool(
        demand_ref_keys(candidate.source_demand_refs) & demand_ref_keys(source_demand_refs)
    )
    is_same_demand = is_same_source or is_same_requisition_line or is_same_root
    if not in_window and not is_same_demand:
        return None

    reasons = [
        "Same vendor",
        "Same item" if same_item else "Same procurement category",
        "Same delivery warehouse",
    ]
    if in_window:
        reasons.append(
            f"Required dates within {window_days} day(s)"
            if candidate.required_date and required_date
            else "One of the two orders has no required date"
        )
    if is_same_source:
        reasons.append(f"Both raised from the same {candidate.source_type.value} demand")
    if is_same_requisition_line:
        reasons.append("Both cover the same pending requisition line")
    if is_same_root:
        reasons.append("Both cover the same sales order / BOM demand")
    has_receipts = candidate.has_receipt_history or Decimal(candidate.received_quantity) > 0
    if not has_receipts:
        reasons.append("Nothing received against the existing order yet")

    if is_same_demand:
        confidence = DuplicateConfidence.EXACT
    elif same_item and candidate.status in _NOT_YET_WITH_VENDOR:
        confidence = DuplicateConfidence.STRONG
    else:
        confidence = DuplicateConfidence.POSSIBLE

    available = available_actions_for(
        status=candidate.status,
        received_quantity=candidate.received_quantity,
        has_receipt_history=has_receipts,
        same_item=same_item,
    )
    recommended = recommend_action(
        status=candidate.status,
        received_quantity=candidate.received_quantity,
        is_same_source=is_same_demand,
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
        item_category_id=candidate.item_category_id,
        has_receipt_history=has_receipts,
        purchase_order_version=candidate.purchase_order_version,
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
    requisition_line_id: uuid.UUID | None = None,
    item_category_id: uuid.UUID | None = None,
    source_demand_refs: Sequence[Mapping[str, object] | DemandReference] = (),
    requisition_line_ids: Collection[uuid.UUID] = (),
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
                requisition_line_id=requisition_line_id,
                item_category_id=item_category_id,
                source_demand_refs=source_demand_refs,
                requisition_line_ids=requisition_line_ids,
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
        f"{strongest.purchase_order_code} already covers the same item or procurement category "
        "for the same vendor, "
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
                    "item_category_id": str(c.item_category_id) if c.item_category_id else None,
                    "status": str(c.status),
                    "has_receipt_history": c.has_receipt_history,
                    "purchase_order_version": c.purchase_order_version,
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
        weights.price * price_score + weights.lead_time * lead_score + weights.rating * rating_score
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
            o.quotation_line_id for o in live if Decimal(o.effective_unit_price) == best_price
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
    return f"{name} scores {best.score} against {len(live)} live quotations: {', '.join(parts)}."


# =============================================================================
# Requisition to purchase orders: the category-wise split
# =============================================================================
#
# An approved requisition becomes one purchase order per (vendor, procurement
# category, warehouse). Every decision on the way is a rule below, so the split
# can be argued with the business without a database: which vendor a line goes
# to, at what price, under which category, as part of which order, and which of
# the vendor's people the order is handed to.

# ASSUMPTION: the vendor-side right that makes a vendor user someone a purchase
# order can be assigned to is ``purchase_order.update`` - the permission the
# portal's acknowledge and shipment actions are gated on today. Held in one
# constant so the dedicated portal right that replaces it is a one-line change
# (docs/assumptions.md §54).
VENDOR_PO_RESPOND_PERMISSION: Permission = Permission.VENDOR_PORTAL_RESPOND

#: One conversion raises at most this many purchase orders. Each order is a
#: bounded run of writes inside the one transaction - an advisory lock, the
#: duplicate check, numbering, the insert, the requisition link and the audit
#: row - so the cost grows with the group count, and twenty orders is already
#: more than a buyer reviews on one screen. A larger split is refused, and the
#: buyer converts fewer requisitions or excludes lines.
MAX_ORDERS_PER_CONVERSION = 20


class RequisitionVendorSource(StrEnum):
    """Where a converted line's vendor came from, strongest source first."""

    #: The buyer named the vendor on the conversion request.
    OVERRIDE = "OVERRIDE"
    #: The requester's ``suggested_vendor_id`` on the requisition line.
    SUGGESTED = "SUGGESTED"
    #: The item's preferred ``item_vendor_links`` row.
    ITEM_PREFERRED = "ITEM_PREFERRED"
    #: The preferred ``vendor_categories`` row for the item's category, or for
    #: its nearest ancestor that has one.
    CATEGORY_PREFERRED = "CATEGORY_PREFERRED"


class RequisitionPriceSource(StrEnum):
    """Where a converted line's unit price came from, strongest source first."""

    OVERRIDE = "OVERRIDE"
    #: The requester's ``estimated_unit_price`` on the requisition line.
    ESTIMATE = "ESTIMATE"
    #: ``item_vendor_links.last_purchase_price`` for that item and vendor.
    LAST_PURCHASE = "LAST_PURCHASE"


class VendorAssigneeReason(StrEnum):
    """Why an order was - or was not - handed to a particular vendor user."""

    #: Exactly one eligible user is scoped to the order's category (or to an
    #: ancestor of it).
    CATEGORY_MATCH = "CATEGORY_MATCH"
    #: Several eligible users would match equally; the buyer picks at Send.
    AMBIGUOUS = "AMBIGUOUS"
    #: Nobody is scoped to the category, and exactly one eligible user is
    #: unrestricted.
    UNRESTRICTED = "UNRESTRICTED"
    #: No eligible user can take the order.
    NO_ELIGIBLE_USER = "NO_ELIGIBLE_USER"


def category_lineage(path: str | None) -> tuple[uuid.UUID, ...]:
    """A category and its ancestors, nearest first, from its materialised path.

    ``item_categories.path`` is ``/<root id>/.../<own id>/``, so the lineage is
    the path's segments read backwards - no query per level. An empty or
    missing path is an empty lineage.
    """
    if not path:
        return ()
    return tuple(reversed([uuid.UUID(segment) for segment in path.split("/") if segment]))


def lineage_from(
    lineage: Sequence[uuid.UUID], category_id: uuid.UUID | None
) -> tuple[uuid.UUID, ...]:
    """The lineage of one member of ``lineage``: that category and what is above it."""
    if category_id is None or category_id not in lineage:
        return ()
    return tuple(lineage[list(lineage).index(category_id) :])


def resolve_procurement_category(
    item_category_lineage: Sequence[uuid.UUID], vendor_category_ids: Collection[uuid.UUID]
) -> uuid.UUID | None:
    """The category a line is bought under from a given vendor.

    ASSUMPTION: the nearest ancestor-or-self of the item's category that the
    vendor is qualified for in ``vendor_categories``; when the vendor is
    qualified for none of them, the item's own category. That is the granularity
    the vendor is engaged at, and it is the same set the vendor's managers are
    scoped to - so the order a manager is shown and the order they are assigned
    can never be drawn at two different levels of the tree
    (docs/assumptions.md §50). An item with no category has none.
    """
    for category_id in item_category_lineage:
        if category_id in vendor_category_ids:
            return category_id
    return item_category_lineage[0] if item_category_lineage else None


def preferred_category_vendor(
    item_category_lineage: Sequence[uuid.UUID], preferred_by_category: Mapping[uuid.UUID, uuid.UUID]
) -> uuid.UUID | None:
    """The preferred vendor of the item's category, or of its nearest ancestor that has one."""
    for category_id in item_category_lineage:
        vendor_id = preferred_by_category.get(category_id)
        if vendor_id is not None:
            return vendor_id
    return None


def resolve_line_vendor(
    *,
    override: uuid.UUID | None,
    suggested: uuid.UUID | None,
    item_preferred: uuid.UUID | None,
    category_preferred: uuid.UUID | None,
) -> tuple[uuid.UUID | None, RequisitionVendorSource | None]:
    """Pick the vendor for one requisition line: the first source that names one.

    ASSUMPTION: the buyer's override, then the requester's suggestion, then the
    item's preferred vendor link, then the preferred vendor of the item's
    category or its nearest ancestor (docs/assumptions.md §51). The person
    closest to the decision wins: a buyer overriding at conversion time knows
    something the master data does not, and a requester who named a vendor did
    so on purpose. ``(None, None)`` is an unresolved line.
    """
    for vendor_id, source in (
        (override, RequisitionVendorSource.OVERRIDE),
        (suggested, RequisitionVendorSource.SUGGESTED),
        (item_preferred, RequisitionVendorSource.ITEM_PREFERRED),
        (category_preferred, RequisitionVendorSource.CATEGORY_PREFERRED),
    ):
        if vendor_id is not None:
            return vendor_id, source
    return None, None


def resolve_line_price(
    *,
    override: Decimal | None,
    estimated: Decimal | None,
    last_purchase: Decimal | None,
) -> tuple[Decimal | None, RequisitionPriceSource | None]:
    """Pick the unit price for one converted line: the first source that has one.

    ASSUMPTION: the buyer's override, then the requester's estimate, then the
    last price paid to the resolved vendor for the item. Never the item's
    ``standard_cost``: that is what the company *values* the item at, not a
    price any vendor offered, and a purchase order printed at it would put an
    internal costing figure on a document the vendor reads
    (docs/assumptions.md §52). A zero is a price; ``None`` is not.
    ``(None, None)`` is an unresolved line.
    """
    for price, source in (
        (override, RequisitionPriceSource.OVERRIDE),
        (estimated, RequisitionPriceSource.ESTIMATE),
        (last_purchase, RequisitionPriceSource.LAST_PURCHASE),
    ):
        if price is not None:
            return Decimal(price), source
    return None, None


def conversion_delivery_date(need_date: date | None, po_date: date) -> date | None:
    """The delivery date a converted line asks the vendor for.

    ASSUMPTION: the requisition's need date, but never earlier than the order
    date. A requirement that is already overdue is wanted as soon as possible,
    and a purchase order cannot promise a delivery before it was raised - the
    database refuses that on the header (docs/assumptions.md §57).
    """
    if need_date is None:
        return None
    return max(need_date, po_date)


def single_order_category(categories: Iterable[uuid.UUID | None]) -> uuid.UUID | None:
    """The one procurement category every line shares, or ``None`` when they differ.

    A line with no category counts as a category of its own, so an order mixing
    a categorised line with an uncategorised one is not claimed for either.
    """
    distinct = set(categories)
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


@dataclass(frozen=True, slots=True)
class PlannedConversionLine:
    """One requisition line, resolved and ready to go onto a purchase order."""

    requisition_id: uuid.UUID
    requisition_code: str
    requisition_line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    description: str | None
    quantity: Decimal
    uom_id: uuid.UUID
    required_date: date | None
    unit_price: Decimal
    price_source: RequisitionPriceSource
    vendor_id: uuid.UUID
    vendor_source: RequisitionVendorSource
    #: The procurement category - see :func:`resolve_procurement_category`.
    item_category_id: uuid.UUID | None
    #: That category and its ancestors, nearest first, for the assignee rule.
    category_lineage: tuple[uuid.UUID, ...]
    warehouse_id: uuid.UUID
    priority: int


@dataclass(frozen=True, slots=True)
class ConversionGroup:
    """The lines that become one purchase order."""

    vendor_id: uuid.UUID
    item_category_id: uuid.UUID | None
    warehouse_id: uuid.UUID
    category_lineage: tuple[uuid.UUID, ...]
    lines: tuple[PlannedConversionLine, ...]

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (str(self.vendor_id), str(self.item_category_id or ""), str(self.warehouse_id))

    @property
    def requisition_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(dict.fromkeys(line.requisition_id for line in self.lines))

    @property
    def source_requisition_id(self) -> uuid.UUID | None:
        """The requisition the order comes from, when all its lines share one."""
        ids = self.requisition_ids
        return ids[0] if len(ids) == 1 else None


def group_conversion_lines(lines: Iterable[PlannedConversionLine]) -> list[ConversionGroup]:
    """One group per (vendor, procurement category, warehouse), deterministically ordered.

    Groups are sorted by ``(str(vendor), str(category or ""), str(warehouse))``
    and the lines inside a group by requisition code then line number, so two
    previews of the same requisitions read the same and two concurrent
    conversions walk their orders in the same sequence.
    """
    buckets: dict[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID], list[PlannedConversionLine]] = {}
    for line in lines:
        buckets.setdefault((line.vendor_id, line.item_category_id, line.warehouse_id), []).append(
            line
        )
    groups = [
        ConversionGroup(
            vendor_id=vendor_id,
            item_category_id=category_id,
            warehouse_id=warehouse_id,
            category_lineage=members[0].category_lineage,
            lines=tuple(sorted(members, key=lambda m: (m.requisition_code, m.line_number))),
        )
        for (vendor_id, category_id, warehouse_id), members in buckets.items()
    ]
    return sorted(groups, key=lambda group: group.sort_key)


def assert_within_order_limit(group_count: int, *, limit: int = MAX_ORDERS_PER_CONVERSION) -> None:
    """Refuse a conversion that would raise more purchase orders than the bound."""
    if group_count > limit:
        raise BusinessRuleError(
            f"This conversion would raise {group_count} purchase orders; at most {limit} can be "
            "raised at once. Convert fewer requisitions, or exclude lines and convert the rest "
            "afterwards.",
            details={"order_count": group_count, "max_orders_per_conversion": limit},
        )


@dataclass(frozen=True, slots=True)
class AssigneeCandidate:
    """A vendor user, as the assignee rule needs to see them."""

    user_id: uuid.UUID
    vendor_id: uuid.UUID | None
    is_active: bool
    is_deleted: bool
    #: Holds :data:`VENDOR_PO_RESPOND_PERMISSION` after roles, grants and
    #: denials are folded (or is a superuser).
    can_respond: bool
    #: Empty means unrestricted - the documented contract of
    #: ``users.allowed_category_ids``.
    allowed_category_ids: tuple[uuid.UUID, ...]


def is_eligible_assignee(candidate: AssigneeCandidate, *, vendor_id: uuid.UUID) -> bool:
    """Active, live, bound to this vendor, and able to act on a purchase order."""
    return (
        candidate.vendor_id == vendor_id
        and candidate.is_active
        and not candidate.is_deleted
        and candidate.can_respond
    )


def scope_covers_category(
    allowed_category_ids: Collection[uuid.UUID], category_lineage: Sequence[uuid.UUID]
) -> bool:
    """Whether a *restricted* scope contains the category or one of its ancestors.

    Subtree semantics: a manager scoped to Handles covers Lever Handles. An
    empty scope is unrestricted and is not a match here - callers decide what
    unrestricted means for them. A category-less order (an empty lineage) is
    covered by no restricted scope.
    """
    return bool(allowed_category_ids) and any(
        category_id in allowed_category_ids for category_id in category_lineage
    )


def may_take_order(
    candidate: AssigneeCandidate, *, vendor_id: uuid.UUID, category_lineage: Sequence[uuid.UUID]
) -> bool:
    """Whether an order on ``vendor_id`` in this category may be assigned to the candidate."""
    if not is_eligible_assignee(candidate, vendor_id=vendor_id):
        return False
    if not candidate.allowed_category_ids:
        return True
    return scope_covers_category(candidate.allowed_category_ids, category_lineage)


def choose_vendor_assignee(
    candidates: Iterable[AssigneeCandidate],
    *,
    vendor_id: uuid.UUID,
    category_lineage: Sequence[uuid.UUID],
) -> tuple[uuid.UUID | None, VendorAssigneeReason]:
    """Pick the vendor user an order on ``vendor_id`` in this category goes to.

    Only eligible users count (:func:`is_eligible_assignee`). Then:

    1. exactly one user scoped to the category, or to an ancestor of it - them;
    2. several such users - nobody, ``AMBIGUOUS``;
    3. none - the one unrestricted user, if there is exactly one;
    4. otherwise nobody.

    ASSUMPTION: an ambiguous match leaves the order unassigned rather than
    guessing; the buyer picks at Send, where the choice is visible
    (docs/assumptions.md §55). A category-less order matches unrestricted
    users only.
    """
    eligible = [c for c in candidates if is_eligible_assignee(c, vendor_id=vendor_id)]
    scoped = [
        c for c in eligible if scope_covers_category(c.allowed_category_ids, category_lineage)
    ]
    if len(scoped) == 1:
        return scoped[0].user_id, VendorAssigneeReason.CATEGORY_MATCH
    if len(scoped) > 1:
        return None, VendorAssigneeReason.AMBIGUOUS

    unrestricted = [c for c in eligible if not c.allowed_category_ids]
    if len(unrestricted) == 1:
        return unrestricted[0].user_id, VendorAssigneeReason.UNRESTRICTED
    if len(unrestricted) > 1:
        return None, VendorAssigneeReason.AMBIGUOUS
    return None, VendorAssigneeReason.NO_ELIGIBLE_USER


# =============================================================================
# Purchase returns: which pool the goods leave
# =============================================================================

#: The two pools a return may draw from, as inventory names them. Mirrors
#: ``models.RETURNABLE_STOCK_STATES``; spelled here so the rule below reads.
RETURN_POOL_AVAILABLE = "AVAILABLE"
RETURN_POOL_DAMAGED = "DAMAGED"


@dataclass(frozen=True, slots=True)
class InspectionOutcome:
    """What an incoming inspection decided about one received line's lot.

    ``accepted`` was released to available stock, ``rejected`` was moved to
    damaged, ``still_held`` (rework and hold) never left QC hold and ``scrap``
    left the position altogether. Only the first two can be returned.
    """

    accepted: Decimal = ZERO
    rejected: Decimal = ZERO
    still_held: Decimal = ZERO
    scrap: Decimal = ZERO

    @property
    def decided(self) -> bool:
        """An undecided inspection carries no disposition at all."""
        return (self.accepted + self.rejected + self.still_held + self.scrap) > ZERO


@dataclass(frozen=True, slots=True)
class ReturnPoolFacts:
    """Everything the pool rule needs to know about one receipt line."""

    #: Taken into stock at receipt - into AVAILABLE, or into QC_HOLD when the
    #: line required inspection.
    accepted_quantity: Decimal
    #: The line was received into QC hold and only an inspection can have moved
    #: it on from there.
    held_for_qc: bool
    #: The inspection's decision, or ``None`` when none is known - no inspection
    #: was raised, the quality module is absent, or it has not decided yet.
    inspection: InspectionOutcome | None = None
    #: Already returned, per pool, by posted returns against this receipt line.
    returned_available: Decimal = ZERO
    returned_damaged: Decimal = ZERO


def returnable_by_pool(facts: ReturnPoolFacts) -> dict[str, Decimal]:
    """How much of a receipt line is still returnable from each pool.

    A line received straight into available stock has nothing in damaged: only
    an inspection puts goods there. A line received into QC hold has whatever
    the inspection released still returnable from available, and whatever it
    rejected still returnable from damaged - each net of what earlier returns
    already took from that pool. Quantity the inspection left on hold, or
    scrapped, is in neither.
    """
    if not facts.held_for_qc:
        return {
            RETURN_POOL_AVAILABLE: facts.accepted_quantity - facts.returned_available,
            RETURN_POOL_DAMAGED: ZERO,
        }
    outcome = facts.inspection
    if outcome is None or not outcome.decided:
        return {RETURN_POOL_AVAILABLE: ZERO, RETURN_POOL_DAMAGED: ZERO}
    return {
        RETURN_POOL_AVAILABLE: outcome.accepted - facts.returned_available,
        RETURN_POOL_DAMAGED: outcome.rejected - facts.returned_damaged,
    }


def resolve_return_pool(
    *,
    line_number: int,
    item_code: str,
    receipt_code: str,
    quantity: Decimal,
    requested_pool: str | None,
    facts: ReturnPoolFacts,
) -> str:
    """Decide - and bound - which pool one return line leaves.

    A named pool is honoured and bounded by what that pool still holds for the
    line. When none is named the pool is resolved from the inspection:

    ASSUMPTION: rejected stock is returned first. The canonical reason for a
    purchase return is goods that failed incoming inspection, so a line whose
    damaged balance covers the whole quantity leaves DAMAGED; otherwise it
    leaves AVAILABLE if that covers it; a quantity that would need both pools is
    refused and the caller asked to split the line and name the pool on each,
    rather than the service guessing a split it would then have to defend.
    """
    if facts.held_for_qc and (facts.inspection is None or not facts.inspection.decided):
        raise BusinessRuleError(
            f"Line {line_number}: {item_code} from {receipt_code} is still on QC hold; "
            "decide the inspection before returning it.",
            details={"line_number": line_number, "item_code": item_code},
        )

    pools = returnable_by_pool(facts)
    if requested_pool is not None:
        if requested_pool not in pools:
            raise ValidationError(
                f"Line {line_number}: a purchase return can only leave "
                f"{RETURN_POOL_AVAILABLE} or {RETURN_POOL_DAMAGED} stock.",
                details={"line_number": line_number, "stock_state": requested_pool},
            )
        remaining = pools[requested_pool]
        if quantity > remaining:
            raise BusinessRuleError(
                f"Line {line_number}: only {to_quantity(max(remaining, ZERO))} of {item_code} "
                f"from {receipt_code} is still returnable from {requested_pool.lower()} stock.",
                details={
                    "line_number": line_number,
                    "stock_state": requested_pool,
                    "returnable_quantity": str(to_quantity(max(remaining, ZERO))),
                    "requested_quantity": str(to_quantity(quantity)),
                },
            )
        return requested_pool

    damaged = pools[RETURN_POOL_DAMAGED]
    available = pools[RETURN_POOL_AVAILABLE]
    if damaged > ZERO and quantity <= damaged:
        return RETURN_POOL_DAMAGED
    if quantity <= available:
        return RETURN_POOL_AVAILABLE
    raise BusinessRuleError(
        f"Line {line_number}: {to_quantity(quantity)} of {item_code} from {receipt_code} "
        f"cannot leave one pool - {to_quantity(max(available, ZERO))} is still returnable "
        f"from available stock and {to_quantity(max(damaged, ZERO))} from damaged. Split "
        "the line and name the stock state on each.",
        details={
            "line_number": line_number,
            "returnable_available": str(to_quantity(max(available, ZERO))),
            "returnable_damaged": str(to_quantity(max(damaged, ZERO))),
            "requested_quantity": str(to_quantity(quantity)),
        },
    )


# =============================================================================
# The incoming-inspection verdict on a receipt line
# =============================================================================


def receipt_line_qc_status(
    *, settled: bool, refused_quantity: Decimal
) -> ReceiptLineQcStatus | None:
    """The QC status an inspection's verdict leaves on its receipt line.

    ``None`` while anything is still on hold for rework or a further decision:
    the line stays PENDING, and so does the purchase order, until the last of
    the lot has been dispositioned.

    ASSUMPTION: a line FAILS incoming inspection when any of its lot was refused
    - rejected or scrapped - and PASSES only when all of it was accepted. A line
    that owes the vendor a return must not read as a clean pass on the receipt,
    however small the refused share (docs/assumptions.md §E4-2).
    """
    if not settled:
        return None
    if refused_quantity > ZERO:
        return ReceiptLineQcStatus.FAILED
    return ReceiptLineQcStatus.PASSED


__all__ = [
    "DEFAULT_SCORE_WEIGHTS",
    "GOODS_RECEIPT_LIFECYCLE",
    "MAX_ORDERS_PER_CONVERSION",
    "PURCHASE_ORDER_LIFECYCLE",
    "PURCHASE_RETURN_LIFECYCLE",
    "QUOTATION_LIFECYCLE",
    "REQUISITION_LIFECYCLE",
    "REQUOTABLE_QUOTATION_STATUSES",
    "RETURN_POOL_AVAILABLE",
    "RETURN_POOL_DAMAGED",
    "RFQ_LIFECYCLE",
    "UNRATED_VENDOR_SCORE",
    "VENDOR_PO_RESPOND_PERMISSION",
    "AssigneeCandidate",
    "CandidateInput",
    "ConversionGroup",
    "DocumentTotals",
    "DuplicateAction",
    "DuplicateCandidate",
    "DuplicateConfidence",
    "InspectionOutcome",
    "LineAmounts",
    "LineReceiptState",
    "OfferInput",
    "PlannedConversionLine",
    "ProposedLine",
    "RankedOffer",
    "RequisitionPriceSource",
    "RequisitionVendorSource",
    "ReturnPoolFacts",
    "ScoreWeights",
    "VendorAssigneeReason",
    "assert_within_order_limit",
    "assert_within_over_receipt_tolerance",
    "available_actions_for",
    "category_lineage",
    "choose_vendor_assignee",
    "classify_candidate",
    "classify_candidates",
    "compute_line_amounts",
    "conversion_delivery_date",
    "dates_overlap",
    "demand_ref_keys",
    "duplicate_refusal",
    "effective_unit_price",
    "group_conversion_lines",
    "is_eligible_assignee",
    "lineage_from",
    "max_receivable_quantity",
    "may_take_order",
    "order_is_fully_received",
    "po_line_status_for",
    "preferred_category_vendor",
    "rank_offers",
    "receipt_line_qc_status",
    "receipt_unit_cost",
    "recommend_action",
    "recommendation_reason",
    "remaining_receivable",
    "requisition_line_status_for",
    "resolve_line_price",
    "resolve_line_vendor",
    "resolve_procurement_category",
    "resolve_return_pool",
    "returnable_by_pool",
    "same_requisition_line",
    "same_source_demand",
    "scope_covers_category",
    "score_offer",
    "single_order_category",
    "sum_document_totals",
]
