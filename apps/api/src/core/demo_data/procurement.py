"""Procurement demonstration data: the buying pipeline, with stock at the end of it.

This is the stage where the demonstration database stops being a catalogue and
starts holding quantity. Everything downstream depends on it: operations cannot
issue material that was never received, quality has nothing to inspect until a
receipt puts something on QC hold, and finance has no vendor invoice to book
until a goods receipt exists to book it against.

What it builds, and why each document is in the state it is
-----------------------------------------------------------
Every procurement screen shows more than one status, because a screen with one
row in one state teaches nobody what the state means:

* **Requisitions** - one DRAFT (still being typed), one PENDING_APPROVAL (in the
  approver's queue), one APPROVED and not yet ordered (the buyer's to-do list),
  and one CONVERTED, because a purchase order was eventually raised from it.
* **RFQs** - one CLOSED, raised from the approved requisition and answered by two
  vendors; one still SENT, with one of its two vendors yet to reply. An open
  enquiry with a partial response set is what the RFQ screen mostly looks like
  in real life.
* **Quotations** - the two answers to the closed RFQ disagree line by line: the
  steel house is keener on the bar, the metals house is keener on the ingot and
  offers a settlement discount on it. So the comparison screen shows a genuine
  trade-off rather than one column that wins everything, and the buyer's recorded
  reason is the record - the score is only its input.
* **Purchase orders** - DRAFT, APPROVED, ACKNOWLEDGED-and-in-transit,
  PARTIALLY_RECEIVED, QC_PENDING, GRN_COMPLETED and short-CLOSED, with delivery
  dates spread either side of the anchor so the overdue filter has rows and the
  forward book has rows.
* **Goods receipts** - seven of them, all posted, which is where stock actually
  enters: ledger, balance, FIFO cost layer and audit row are all written by
  ``inventory.post_movements`` because the receipt path calls it.
* **Purchase returns** - one posted (which reverses stock against the same lot it
  came in on) and one DRAFT.

The batch-tracked lot, and the serial-tracked gap
-------------------------------------------------
The clear lacquer is received twice under two different vendor lot numbers, one
of them flagged for incoming inspection. That gives the batch screen two rows for
one item, the QC inbox a real inspection, and the traceability rules a lot to
follow - and it lands the same item in two stock states at once, which is the
distinction ``docs/assumptions.md`` section 23 turns on.

There is deliberately **no serialised receipt**, and this is not an oversight:

* ``procurement.service`` refuses a serial-tracked item on a goods receipt line
  outright (``SERIAL_RECEIPT_MESSAGE``) - a receipt line has nowhere to record
  the many serials that arrive against one line, and the inventory engine
  requires a serial id on every movement of such an item;
* the only serial-tracked demonstration item is a manufactured finished good, so
  ``can_purchase`` is false on it and it could not go on a purchase order even if
  the receipt path allowed it.

Both facts are recorded in ``ctx.skipped`` at run time rather than worked around
here. Serialised stock is registered through inventory, which is where the units
are actually born, and that belongs to a later stage.

Where the stock lands
---------------------
Receipts are posted **without a storage location**, at the warehouse grain.
``BalanceGrain`` includes ``location_id``, so material received into a bin can
only be issued from that bin; choosing bins here would silently commit every
later stage to knowing which ones. The bins the masters stage created are real
and a bin transfer can move stock into them later - that is a separate decision,
not one this file should make on operations' behalf.

Levels are set so the operations stage can issue against a production order for
every raw material and bought-out component, with exactly one deliberate
exception: the **pin cylinder** arrives 250 against 3 000 ordered, the vendor
withdraws the balance, and the order is short-closed. That leaves 250 on hand
against a reorder level of 1 200 and *nothing on order* - a shortage the reorder
report and MRP both have to show, which they would not if a live purchase order
were still covering it.

What later stages address, and by what code
-------------------------------------------
Everything created *or found* is registered, because a later stage cannot tell
which happened and must not have to:

======================  =====================================================
kind                    codes
======================  =====================================================
``requisition``         ``DM-PR-METALS``, ``DM-PR-PACK``, ``DM-PR-COMP``,
                        ``DM-PR-CONSUM``
``rfq``                 ``DM-RFQ-METALS`` (closed), ``DM-RFQ-BRASS`` (open)
``quotation``           ``DM-QT-METALS-SMA``, ``DM-QT-METALS-DSC``,
                        ``DM-QT-BRASS-SMA``
``purchase_order``      ``DM-PO-RFQ``, ``DM-PO-METALS``, ``DM-PO-COMP``,
                        ``DM-PO-CYL``, ``DM-PO-PACK``, ``DM-PO-LACQUER``,
                        ``DM-PO-ZINC``, ``DM-PO-BRASS``, ``DM-PO-DRAFT``
``goods_receipt``       ``DM-GRN-METALS``, ``DM-GRN-CYL``, ``DM-GRN-COMP``,
                        ``DM-GRN-PACK``, ``DM-GRN-LAC-A``, ``DM-GRN-RFQ``,
                        ``DM-GRN-LAC-B``
``purchase_return``     ``DM-PRTN-ZINC`` (posted), ``DM-PRTN-SPRING`` (draft)
======================  =====================================================

Idempotence without a predictable code
--------------------------------------
Every document here takes its ``code`` from the company's document sequence
(``PR-2026-00014``), so it is not predictable and ``find_by_code`` cannot be the
idempotence key the way it is in the masters stage. The marker goes in the first
token of ``notes`` instead - ``[DM-PO-METALS] Quarterly ...`` - and one query per
document type recovers the whole set. The quotation is the exception: the service
already enforces one quotation per (RFQ, vendor), so that pair *is* the identity
and inventing a second key for it would be able to disagree with the service's.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from src.core.clock import utcnow
from src.core.demo_data import DEMO_PREFIX, DemoContext
from src.core.errors import BusinessRuleError, ValidationError
from src.modules.procurement import service as procurement
from src.modules.procurement.models import (
    GoodsReceipt,
    GoodsReceiptStatus,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequisition,
    PurchaseReturn,
    PurchaseReturnStatus,
    PurchaseSourceType,
    QuotationStatus,
    RequisitionStatus,
    Rfq,
    RfqStatus,
    VendorQuotation,
)
from src.modules.procurement.schemas import (
    GoodsReceiptCreate,
    GoodsReceiptLineIn,
    PurchaseOrderCreate,
    PurchaseOrderLineIn,
    PurchaseReturnCreate,
    PurchaseReturnLineIn,
    QuotationCreate,
    QuotationLineIn,
    RequisitionCreate,
    RequisitionLineIn,
    RfqCreate,
    RfqLineIn,
)
from src.modules.products.models import Item

STAGE = "procurement"


# -----------------------------------------------------------------------------
# Dates
# -----------------------------------------------------------------------------
# Every date in this file is an offset in days from one anchor. Nothing calls the
# clock twice: two documents that should share a date must share it exactly, and
# a second reading of "today" across a midnight boundary is how a re-run stops
# being idempotent.
#
# A function rather than a module constant, for the same reason the engineering
# stage uses one: a constant is evaluated at import time, which is a different
# moment from the run under a frozen clock.


def _anchor() -> date:
    """The date every other date here is measured from."""
    return utcnow().date()


def _on(days: int) -> date:
    """``days`` from the anchor. Negative is the past, positive is the future."""
    return _anchor() + timedelta(days=days)


# -----------------------------------------------------------------------------
# The idempotence marker
# -----------------------------------------------------------------------------

_MARKER_OPEN = "["
_MARKER_CLOSE = "]"
#: Matches the marker of every document this file writes, and nothing else.
_MARKER_LIKE = f"{_MARKER_OPEN}{DEMO_PREFIX}-%"


def _tagged(marker: str, text: str) -> str:
    """A document note that carries its demonstration marker as the first token."""
    return f"{_MARKER_OPEN}{marker}{_MARKER_CLOSE} {text}"


def _marker_of(notes: str | None) -> str | None:
    if not notes or not notes.startswith(_MARKER_OPEN):
        return None
    end = notes.find(_MARKER_CLOSE)
    return notes[1:end] if end > 1 else None


async def _existing(ctx: DemoContext, model: Any) -> dict[str, Any]:
    """Every demonstration document of one type, keyed by its marker.

    One query per document type rather than one per document: the whole set is
    small and the alternative is a round trip for each of thirty documents on
    every re-run, which is exactly the N+1 this codebase treats as a defect.
    """
    stmt = select(model).where(model.company_id == ctx.company_id, model.notes.like(_MARKER_LIKE))
    found: dict[str, Any] = {}
    for row in (await ctx.session.execute(stmt)).scalars():
        marker = _marker_of(row.notes)
        if marker is not None:
            found[marker] = row
    return found


def _seen(ctx: DemoContext, kind: str, code: str, row_id: uuid.UUID, *, created: bool) -> uuid.UUID:
    """Register a document and count it, whether this run made it or found it.

    Both cases register: a later stage cannot tell which happened and must not
    have to, which is the whole basis of re-running this tool.
    """
    ctx.note(STAGE, created=1 if created else 0, existing=0 if created else 1)
    return ctx.remember(kind, code, row_id)


def _item(ctx: DemoContext, code: str) -> uuid.UUID:
    """An item the masters stage registered, addressed by its unprefixed suffix."""
    return ctx.id_of("item", ctx.code(*code.split("-")))


def _vendor(ctx: DemoContext, code: str) -> uuid.UUID:
    return ctx.id_of("vendor", ctx.code(*code.split("-")))


# -----------------------------------------------------------------------------
# Purchase requisitions
# -----------------------------------------------------------------------------
# Four of them, one per status the approval screen has to be able to show. The
# quantities are what a works of this size actually asks for: metal by the
# quarter-tonne, fasteners by the tens of thousands, cartons by the pallet.


@dataclass(frozen=True, slots=True)
class _RequisitionLine:
    item: str
    quantity: str
    #: The requester's expectation, never a committed price. Deliberately a
    #: little off the eventual order price - that gap is the point of the field.
    estimated_unit_price: str
    suggested_vendor: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class _Requisition:
    marker: tuple[str, ...]
    warehouse: str
    raised_in: int
    required_in: int
    priority: int
    purpose: str
    notes: str
    #: How far up the approval chain this one is walked.
    stop_at: RequisitionStatus
    lines: tuple[_RequisitionLine, ...]


REQUISITIONS: tuple[_Requisition, ...] = (
    # Approved, and the only one an RFQ and then an order is raised from - so it
    # ends the stage CONVERTED. The required date is in the recent past because
    # the material it asked for has since arrived, slightly late, which is what
    # the requisition-to-receipt trace should read like.
    _Requisition(
        marker=("PR", "METALS"),
        warehouse="WH-RM",
        raised_in=-34,
        required_in=-6,
        priority=3,
        purpose="Bar and ingot for the Q3 lever and lockset build",
        notes="Raised off the production plan; three-vendor enquiry requested.",
        stop_at=RequisitionStatus.APPROVED,
        lines=(
            _RequisitionLine(
                item="RM-SS",
                quantity="600",
                estimated_unit_price="288.00",
                suggested_vendor="VEN-SS",
                notes="Bright bar, 10mm. Mill test certificate required with the lot.",
            ),
            _RequisitionLine(
                item="RM-ZINC",
                quantity="500",
                estimated_unit_price="268.00",
                suggested_vendor="VEN-BRASS",
                notes="ZA-5 only. The die caster rejects re-melt with tramp aluminium.",
            ),
        ),
    ),
    # Approved and sitting in the buyer's queue with no order against it. An
    # approved requisition nobody has acted on yet is the normal state of the
    # buying desk, and the "approved, not ordered" list is the screen that
    # matters most to a purchase manager.
    _Requisition(
        marker=("PR", "PACK"),
        warehouse="WH-PKG",
        raised_in=-8,
        required_in=12,
        priority=5,
        purpose="Cartons and bags for the festive despatch season",
        notes="Artwork unchanged from the last print run.",
        stop_at=RequisitionStatus.APPROVED,
        lines=(
            _RequisitionLine(
                item="PK-BOX",
                quantity="6000",
                estimated_unit_price="14.50",
                suggested_vendor="VEN-PACK",
            ),
            _RequisitionLine(
                item="PK-POLYBAG",
                quantity="25000",
                estimated_unit_price="0.95",
                suggested_vendor="VEN-PACK",
            ),
        ),
    ),
    # In the approver's inbox. Priority 2 so the pending-approvals queue has
    # something at the top of it that is not simply the oldest row.
    _Requisition(
        marker=("PR", "COMP"),
        warehouse="WH-CMP",
        raised_in=-4,
        required_in=18,
        priority=2,
        purpose="Component top-up - cylinder stock is below the reorder level",
        notes="Cylinders are the constraint on the 6572 lockset; see the shortage report.",
        stop_at=RequisitionStatus.PENDING_APPROVAL,
        lines=(
            _RequisitionLine(
                item="BO-CYL-60",
                quantity="2000",
                estimated_unit_price="171.00",
                suggested_vendor="VEN-FAST",
                notes="Chase the balance the last order never delivered.",
            ),
            _RequisitionLine(
                item="BO-SCREW-CSK",
                quantity="30000",
                estimated_unit_price="1.82",
                suggested_vendor="VEN-FAST",
            ),
            _RequisitionLine(
                item="BO-SPRING",
                quantity="12000",
                estimated_unit_price="4.15",
                suggested_vendor="VEN-FAST",
            ),
        ),
    ),
    # Still being typed. A draft with no approver named is what the "my drafts"
    # view is for, and a requisition list with nothing in DRAFT looks like a
    # system nobody uses.
    _Requisition(
        marker=("PR", "CONSUM"),
        warehouse="WH-RM",
        raised_in=-1,
        required_in=21,
        priority=6,
        purpose="Finishing shop consumables",
        notes="Awaiting the finishing supervisor's confirmation on the lacquer quantity.",
        stop_at=RequisitionStatus.DRAFT,
        lines=(
            _RequisitionLine(
                item="CN-LACQUER",
                quantity="120",
                estimated_unit_price="415.00",
                notes="Air-drying, same shade as lot LQ-2612-B.",
            ),
            _RequisitionLine(
                item="RM-BRASS",
                quantity="300",
                estimated_unit_price="620.00",
                suggested_vendor="VEN-BRASS",
            ),
        ),
    ),
)

#: DRAFT is where a requisition starts, so the walk below only ever moves
#: forward through these. A requisition that has already been CONVERTED by a
#: purchase order is not on this route and is left exactly where it is.
_REQUISITION_ROUTE: tuple[RequisitionStatus, ...] = (
    RequisitionStatus.DRAFT,
    RequisitionStatus.PENDING_APPROVAL,
    RequisitionStatus.APPROVED,
)

_REQUISITION_REASON: dict[RequisitionStatus, str] = {
    RequisitionStatus.PENDING_APPROVAL: "Quantities checked against the plan; sent for approval",
    RequisitionStatus.APPROVED: "Within the department's budget for the quarter; approved",
}


# -----------------------------------------------------------------------------
# Requests for quotation
# -----------------------------------------------------------------------------
# Two enquiries, both to the two metal stockists, because those are the only two
# vendors on the approved list whose categories genuinely overlap - a component
# house cannot quote on bar, and an enquiry sent to a vendor who cannot supply
# the item is noise on both desks.


@dataclass(frozen=True, slots=True)
class _RfqLine:
    item: str
    quantity: str
    specification: str | None = None


@dataclass(frozen=True, slots=True)
class _Rfq:
    marker: tuple[str, ...]
    #: Raise it from an approved requisition, letting the service copy the open
    #: lines across, or state the lines outright. Both paths exist in the
    #: service and both are worth having on screen.
    requisition: tuple[str, ...] | None
    vendors: tuple[str, ...]
    raised_in: int
    response_due_in: int
    required_in: int
    terms: str
    notes: str
    #: Closed once the answers are in and one has been chosen.
    close_after_quotes: bool
    lines: tuple[_RfqLine, ...] = ()


RFQS: tuple[_Rfq, ...] = (
    _Rfq(
        marker=("RFQ", "METALS"),
        requisition=("PR", "METALS"),
        vendors=("VEN-SS", "VEN-BRASS"),
        raised_in=-32,
        response_due_in=-26,
        required_in=-6,
        terms=(
            "Ex-works Mumbai or Jamnagar. Prices to hold for 30 days. "
            "Mill test certificate with every lot. Rejection at our works is to "
            "the supplier's account."
        ),
        notes="Both houses stock all three metals; asked both to price both lines.",
        close_after_quotes=True,
    ),
    # Open. One of the two vendors has answered and the other has not, which is
    # what an RFQ that is still running actually looks like.
    _Rfq(
        marker=("RFQ", "BRASS"),
        requisition=None,
        vendors=("VEN-BRASS", "VEN-SS"),
        raised_in=-6,
        response_due_in=4,
        required_in=30,
        terms="Delivered to our Rajkot works. Payment 45 days from receipt.",
        notes="Next quarter's bar and ingot. Deepak Steel yet to respond.",
        close_after_quotes=False,
        lines=(
            _RfqLine(
                item="RM-BRASS",
                quantity="1500",
                specification="IS 319 free-cutting, 12mm, +/- 0.05mm, in 3m lengths.",
            ),
            _RfqLine(
                item="RM-ZINC",
                quantity="600",
                specification="ZA-5 virgin ingot. No secondary alloy.",
            ),
        ),
    ),
)


# -----------------------------------------------------------------------------
# Vendor quotations
# -----------------------------------------------------------------------------
# The two answers to the closed enquiry disagree line by line on purpose. Deepak
# Steel is keener on the bar - it is their metal - and Shree is keener on the
# ingot and offers a settlement discount on it, but wants more for freight and is
# slower on the bar. So neither column wins outright, the comparison screen has a
# real decision on it, and the buyer's recorded reason is what the audit trail
# keeps. Prices are per kilogram in rupees and sit within a few percent of the
# item master's standard cost, which is where a live quotation sits.


@dataclass(frozen=True, slots=True)
class _Offer:
    item: str
    unit_price: str
    lead_time_days: int
    discount_percentage: str | None = None
    min_order_quantity: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class _Quotation:
    marker: tuple[str, ...]
    rfq: tuple[str, ...]
    vendor: str
    #: The vendor's own reference. Their document number, not ours.
    vendor_reference: str
    quoted_in: int
    valid_until_in: int
    payment_terms_days: int
    lead_time_days: int
    freight: str
    terms: str
    notes: str
    #: SELECTED, REJECTED, or ``None`` to leave it RECEIVED and undecided.
    decision: QuotationStatus | None
    decision_reason: str | None
    offers: tuple[_Offer, ...]


QUOTATIONS: tuple[_Quotation, ...] = (
    _Quotation(
        marker=("QT", "METALS", "DSC"),
        rfq=("RFQ", "METALS"),
        vendor="VEN-SS",
        vendor_reference="DSC/Q/8871",
        quoted_in=-25,
        # 30 days, which is the usual life of a metal quotation. Measured from
        # the anchor rather than from a fixed date, so it is never already
        # lapsed when the tool runs and the selection below cannot be refused.
        valid_until_in=20,
        payment_terms_days=30,
        lead_time_days=12,
        freight="1200.00",
        terms="Ex-works Mumbai. 30 days from invoice. Prices firm for 30 days.",
        notes="Confirmed by e-mail; rates hold if the whole enquiry is placed together.",
        decision=QuotationStatus.SELECTED,
        decision_reason=(
            "Cheapest on the bar, which is three-quarters of the value, and half the "
            "freight. Loses on the ingot but wins the enquiry overall."
        ),
        offers=(
            _Offer(item="RM-SS", unit_price="278.500000", lead_time_days=12),
            _Offer(
                item="RM-ZINC",
                unit_price="266.000000",
                lead_time_days=16,
                notes="Bought in; longer lead time than our own stock lines.",
            ),
        ),
    ),
    _Quotation(
        marker=("QT", "METALS", "SMA"),
        rfq=("RFQ", "METALS"),
        vendor="VEN-BRASS",
        vendor_reference="SMA/QT/2419",
        quoted_in=-24,
        valid_until_in=20,
        payment_terms_days=45,
        lead_time_days=15,
        freight="2400.00",
        terms="Ex-works Jamnagar. 45 days. 2% settlement discount on zinc only.",
        notes="Their own metal is the ingot; the bar is a traded line for them.",
        decision=QuotationStatus.REJECTED,
        decision_reason=(
            "Best on zinc even after their discount, but 13.50/kg dearer on the bar "
            "and double the freight. Splitting the enquiry was not worth two deliveries."
        ),
        offers=(
            _Offer(
                item="RM-SS",
                unit_price="292.000000",
                lead_time_days=15,
                notes="Traded line; priced off their Mumbai supplier.",
            ),
            _Offer(
                item="RM-ZINC",
                unit_price="259.000000",
                lead_time_days=10,
                discount_percentage="2",
                min_order_quantity="500",
            ),
        ),
    ),
    # The single answer to the open enquiry. Left RECEIVED: nobody can decide an
    # enquiry that is still waiting on its second vendor.
    _Quotation(
        marker=("QT", "BRASS", "SMA"),
        rfq=("RFQ", "BRASS"),
        vendor="VEN-BRASS",
        vendor_reference="SMA/QT/2588",
        quoted_in=-3,
        valid_until_in=25,
        payment_terms_days=45,
        lead_time_days=10,
        freight="3000.00",
        terms="Delivered Rajkot. 45 days. Prices linked to the LME on the order date.",
        notes="Verbal indication that they will hold the rate if we place by month end.",
        decision=None,
        decision_reason=None,
        offers=(
            _Offer(item="RM-BRASS", unit_price="617.000000", lead_time_days=10),
            _Offer(
                item="RM-ZINC",
                unit_price="261.500000",
                lead_time_days=10,
                min_order_quantity="500",
            ),
        ),
    ),
)


# -----------------------------------------------------------------------------
# Purchase orders
# -----------------------------------------------------------------------------
# Nine orders. Delivery dates are spread either side of the anchor so the overdue
# filter has rows and the forward order book has rows, and no two open orders put
# the same item on the same vendor and warehouse inside the seven-day duplicate
# window - the duplicate check is a rule this file has to respect rather than
# acknowledge its way past, and a demo database full of acknowledged duplicates
# would teach the wrong lesson about section 16.


@dataclass(frozen=True, slots=True)
class _OrderLine:
    item: str
    quantity: str
    unit_price: str
    required_in: int
    requires_qc: bool = False
    discount_percentage: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class _Order:
    marker: tuple[str, ...]
    vendor: str
    warehouse: str
    raised_in: int
    expected_in: int
    priority: int
    freight: str
    terms: str
    notes: str
    #: Where the walk stops before any receipt is posted against it.
    stop_at: PurchaseOrderStatus
    lines: tuple[_OrderLine, ...]
    vendor_reference: str | None = None
    delivery_address: str | None = None
    #: Inbound courier details, as ``(courier, tracking reference, days ago)``.
    #: Only meaningful on an order the vendor has already despatched.
    shipment: tuple[str, str, int] | None = None


#: Where every order that is *not* raised from a quotation comes from. The
#: quotation-sourced one is built separately, from the quotation's own lines, so
#: the price on the order is the price that was quoted rather than a second copy
#: of it that can drift.
ORDERS: tuple[_Order, ...] = (
    # Delivered and complete. Zinc is short-shipped by 20kg at the gate - goods
    # refused on arrival never enter stock, so the receipt records them as
    # rejected and the ledger never sees them.
    _Order(
        marker=("PO", "METALS"),
        vendor="VEN-BRASS",
        warehouse="WH-RM",
        raised_in=-46,
        expected_in=-30,
        priority=4,
        freight="2800.00",
        terms="45 days from receipt. Ex-works Jamnagar, freight to our account.",
        notes="Quarterly bar and ingot call-off against the running rate contract.",
        stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
        vendor_reference="SMA/SO/11902",
        delivery_address="Atcraft Hardware, Survey 118, Shapar Veraval, Rajkot 360024",
        shipment=("VRL Logistics", "VRL-8842317", 33),
        lines=(
            _OrderLine(item="RM-BRASS", quantity="1200", unit_price="608.000000", required_in=-30),
            _OrderLine(item="RM-ZINC", quantity="800", unit_price="264.000000", required_in=-30),
        ),
    ),
    # Partially received: the screws came in full, the springs came in at 12 000
    # of 20 000. The balance is still expected, so the order stays open and shows
    # on the overdue list - its promised date was three weeks ago.
    _Order(
        marker=("PO", "COMP"),
        vendor="VEN-FAST",
        warehouse="WH-CMP",
        raised_in=-38,
        expected_in=-22,
        priority=4,
        freight="900.00",
        terms="30 days from receipt. Delivered to our component store.",
        notes="Fastener call-off. Vendor is reliable on quantity, slow on paperwork.",
        stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
        vendor_reference="PF/SO/44018",
        lines=(
            _OrderLine(
                item="BO-SCREW-CSK", quantity="50000", unit_price="1.780000", required_in=-22
            ),
            _OrderLine(
                item="BO-SPRING",
                quantity="20000",
                unit_price="4.050000",
                required_in=-22,
                notes="Free length 18.5mm +/- 0.3. Reject the last lot's variation.",
            ),
        ),
    ),
    # The cylinder order, and the whole reason the shortage report has something
    # on it. 250 of 3 000 arrived, the vendor then withdrew the balance, and the
    # order is short-closed further down - so the item ends with 250 on hand
    # against a reorder level of 1 200 and nothing on order to cover it.
    _Order(
        marker=("PO", "CYL"),
        vendor="VEN-FAST",
        warehouse="WH-CMP",
        raised_in=-34,
        expected_in=-20,
        priority=3,
        freight="0",
        terms="30 days from receipt. Delivered.",
        notes="Cylinders for the 6572 lockset build.",
        stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
        vendor_reference="PF/SO/44031",
        lines=(
            _OrderLine(
                item="BO-CYL-60",
                quantity="3000",
                unit_price="164.000000",
                required_in=-20,
                notes="5-pin, brass body, keyed to differ.",
            ),
        ),
    ),
    _Order(
        marker=("PO", "PACK"),
        vendor="VEN-PACK",
        warehouse="WH-PKG",
        raised_in=-27,
        expected_in=-15,
        priority=6,
        freight="1500.00",
        terms="21 days from receipt. Delivered to our packing store.",
        notes="Cartons and bags for the current despatch programme.",
        stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
        vendor_reference="KP/SO/7719",
        lines=(
            _OrderLine(item="PK-BOX", quantity="6000", unit_price="14.200000", required_in=-15),
            _OrderLine(item="PK-POLYBAG", quantity="25000", unit_price="0.910000", required_in=-15),
        ),
    ),
    # The batch-tracked line. `requires_qc` is set on the order line so the shade
    # of an incoming lacquer lot is checked before it can be issued - and the
    # second delivery below overrides it back off, which is the field's whole
    # purpose: the order line states the policy, the receipt line states what
    # happened on the day.
    _Order(
        marker=("PO", "LACQUER"),
        vendor="VEN-PLATE",
        warehouse="WH-RM",
        raised_in=-24,
        expected_in=-12,
        priority=5,
        freight="0",
        terms="15 days from receipt. Collected from their Rajkot shed.",
        notes="Clear lacquer for the finishing line; the platers supply it with the plating.",
        # Left SENT rather than acknowledged - a plating shop confirms nothing in
        # writing, and an order that was never acknowledged still has to be
        # receivable against. That edge is an explicit assumption in the state
        # machine and is worth having a live example of.
        stop_at=PurchaseOrderStatus.SENT,
        lines=(
            _OrderLine(
                item="CN-LACQUER",
                quantity="240",
                unit_price="405.000000",
                required_in=-12,
                requires_qc=True,
                notes="Shade to match the approved panel. Lot number on every tin.",
            ),
        ),
    ),
    # In transit. Acknowledged, on a lorry, nothing received - so the inbound
    # tracking screen has a live consignment on it.
    _Order(
        marker=("PO", "ZINC"),
        vendor="VEN-BRASS",
        warehouse="WH-RM",
        raised_in=-9,
        expected_in=18,
        priority=4,
        freight="1800.00",
        terms="45 days from receipt. Ex-works Jamnagar.",
        notes="Ingot top-up ahead of the festive lockset run.",
        stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
        vendor_reference="SMA/SO/12044",
        shipment=("Gati Express", "GATI-5510992", 2),
        lines=(
            _OrderLine(item="RM-ZINC", quantity="600", unit_price="262.000000", required_in=18),
        ),
    ),
    # Approved and not yet sent: the buyer is holding it until the brass enquiry
    # closes. An approved order waiting to go out is a real state and the send
    # queue should have something in it.
    _Order(
        marker=("PO", "BRASS"),
        vendor="VEN-BRASS",
        warehouse="WH-RM",
        raised_in=-3,
        expected_in=24,
        priority=5,
        freight="2200.00",
        terms="45 days from receipt. Ex-works Jamnagar.",
        notes="Holding despatch until the open brass enquiry closes.",
        stop_at=PurchaseOrderStatus.APPROVED,
        lines=(
            _OrderLine(item="RM-BRASS", quantity="800", unit_price="615.000000", required_in=24),
        ),
    ),
    # Still a draft. Two lines, no approver, nothing committed.
    _Order(
        marker=("PO", "DRAFT"),
        vendor="VEN-FAST",
        warehouse="WH-CMP",
        raised_in=-1,
        expected_in=28,
        priority=5,
        freight="900.00",
        terms="30 days from receipt.",
        notes="Next quarter's fastener call-off. Rates to be confirmed before release.",
        stop_at=PurchaseOrderStatus.DRAFT,
        lines=(
            _OrderLine(item="BO-SPRING", quantity="15000", unit_price="4.050000", required_in=28),
            _OrderLine(
                item="BO-SCREW-CSK", quantity="25000", unit_price="1.780000", required_in=28
            ),
        ),
    ),
)

#: The pre-receipt run of the purchase order lifecycle, in order. A walk only
#: ever moves forward along it; an order already past ACKNOWLEDGED - because a
#: previous run received against it - is not on the route and is left alone.
_ORDER_ROUTE: tuple[PurchaseOrderStatus, ...] = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
)

_ORDER_REASON: dict[PurchaseOrderStatus, str] = {
    PurchaseOrderStatus.PENDING_APPROVAL: "Priced and checked against the enquiry; submitted",
    PurchaseOrderStatus.APPROVED: "Within the purchase manager's delegated limit; approved",
    PurchaseOrderStatus.SENT: "Order e-mailed to the vendor with the drawing pack",
    PurchaseOrderStatus.ACKNOWLEDGED: "Vendor confirmed the quantity and the delivery week",
}

#: The order the quotation chain ends in, kept beside the quotation it is priced
#: from so the two cannot be edited apart.
SOURCED_ORDER = _Order(
    marker=("PO", "RFQ"),
    vendor="VEN-SS",
    warehouse="WH-RM",
    raised_in=-20,
    expected_in=-6,
    priority=3,
    freight="1200.00",
    terms="30 days from invoice. Ex-works Mumbai. Prices per quotation DSC/Q/8871.",
    notes="Placed on the selected quotation; requisition, enquiry and offer all linked.",
    stop_at=PurchaseOrderStatus.ACKNOWLEDGED,
    vendor_reference="DSC/SO/3390",
    shipment=("VRL Logistics", "VRL-8901554", 7),
    lines=(),
)

#: Which quotation :data:`SOURCED_ORDER` is raised from, and when its lines are
#: wanted. Everything else about the lines - quantity, price, discount - is read
#: off the quotation itself.
SOURCED_FROM = ("QT", "METALS", "DSC")
SOURCED_REQUIRED_IN = -6


# -----------------------------------------------------------------------------
# Goods receipts
# -----------------------------------------------------------------------------
# This is where stock enters the system. Every one of these is posted through
# `procurement.post_goods_receipt`, which calls `inventory.post_movements`, so
# the ledger row, the balance, the FIFO cost layer, the batch and the audit entry
# are all written together by the code the application itself runs. Nothing here
# touches a balance directly.


@dataclass(frozen=True, slots=True)
class _ReceiptLine:
    item: str
    received: str
    accepted: str
    rejected: str = "0"
    rejection_reason: str | None = None
    batch_number: str | None = None
    manufactured_in: int | None = None
    expires_in: int | None = None
    #: Overrides the order line's inspection policy for this delivery.
    requires_qc: bool | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class _Receipt:
    marker: tuple[str, ...]
    order: tuple[str, ...]
    received_in: int
    delivery_note: str
    invoice_number: str
    gate_entry: str
    vehicle: str
    transporter: str | None
    notes: str
    lines: tuple[_ReceiptLine, ...]


#: In date order, because that is the order the storekeeper booked them in and
#: because posting order decides which FIFO layer a later issue consumes first.
RECEIPTS: tuple[_Receipt, ...] = (
    _Receipt(
        marker=("GRN", "METALS"),
        order=("PO", "METALS"),
        received_in=-29,
        delivery_note="SMA/DC/4471",
        invoice_number="SMA/INV/2211",
        gate_entry="GE-4471",
        vehicle="GJ 03 AX 8841",
        transporter="DM-TRN-VRL",
        notes="Weighbridge slip attached. Two ingot bundles refused at the gate.",
        lines=(
            _ReceiptLine(
                item="RM-BRASS",
                received="1200",
                accepted="1200",
                notes="Mill test certificate received with the lot.",
            ),
            _ReceiptLine(
                item="RM-ZINC",
                received="800",
                accepted="780",
                rejected="20",
                rejection_reason="Two ingots surface-oxidised and water-marked in transit.",
            ),
        ),
    ),
    _Receipt(
        marker=("GRN", "COMP"),
        order=("PO", "COMP"),
        received_in=-21,
        delivery_note="PF/DC/9912",
        invoice_number="PF/INV/8830",
        gate_entry="GE-9912",
        vehicle="MH 12 QR 4471",
        transporter="DM-TRN-GATI",
        notes="Springs part-shipped; the balance of 8 000 is still due.",
        lines=(
            _ReceiptLine(item="BO-SCREW-CSK", received="50000", accepted="50000"),
            _ReceiptLine(
                item="BO-SPRING",
                received="12000",
                accepted="11850",
                rejected="150",
                rejection_reason="One carton burred at the cut end; free length out of tolerance.",
            ),
        ),
    ),
    _Receipt(
        marker=("GRN", "CYL"),
        order=("PO", "CYL"),
        received_in=-19,
        delivery_note="PF/DC/9945",
        invoice_number="PF/INV/8851",
        gate_entry="GE-9945",
        vehicle="MH 12 QR 4471",
        transporter="DM-TRN-GATI",
        notes="Token delivery only. Vendor has since withdrawn the balance.",
        lines=(
            _ReceiptLine(
                item="BO-CYL-60",
                received="250",
                accepted="250",
                notes="Keyed to differ, as ordered.",
            ),
        ),
    ),
    _Receipt(
        marker=("GRN", "PACK"),
        order=("PO", "PACK"),
        received_in=-14,
        delivery_note="KP/DC/3312",
        invoice_number="KP/INV/2205",
        gate_entry="GE-3312",
        vehicle="GJ 03 BT 1190",
        # Not DM-TRN-CITY: masters retires that carrier deliberately, and a
        # retired one cannot be named on a live document.
        transporter="DM-TRN-GATI",
        notes="Print quality checked against the approved proof.",
        lines=(
            _ReceiptLine(item="PK-BOX", received="6000", accepted="6000"),
            _ReceiptLine(item="PK-POLYBAG", received="25000", accepted="25000"),
        ),
    ),
    # The first lacquer lot, held for incoming inspection. The quantity is posted
    # into QC_HOLD rather than AVAILABLE, so it is physically present, visible in
    # valuation, and cannot be promised to anything until quality releases it.
    _Receipt(
        marker=("GRN", "LAC", "A"),
        order=("PO", "LACQUER"),
        received_in=-11,
        delivery_note="CE/DC/1180",
        invoice_number="CE/INV/0994",
        gate_entry="GE-1180",
        vehicle="GJ 03 CC 2277",
        transporter=None,
        notes="First half of the order. Shade check raised before the tins are opened.",
        lines=(
            _ReceiptLine(
                item="CN-LACQUER",
                received="120",
                accepted="120",
                batch_number="LQ-2609-A",
                manufactured_in=-40,
                # The item carries a 365-day shelf life, so the lot expires a
                # year from the day it was made, not a year from today.
                expires_in=325,
                requires_qc=True,
                notes="Lot certificate quotes a slightly warmer shade than the panel.",
            ),
        ),
    ),
    _Receipt(
        marker=("GRN", "RFQ"),
        order=("PO", "RFQ"),
        received_in=-5,
        delivery_note="DSC/DC/6604",
        invoice_number="DSC/INV/5512",
        gate_entry="GE-6604",
        vehicle="MH 04 KL 7781",
        transporter="DM-TRN-VRL",
        notes="Both lines complete. Test certificates filed against the lot.",
        lines=(
            _ReceiptLine(item="RM-SS", received="600", accepted="600"),
            _ReceiptLine(item="RM-ZINC", received="500", accepted="500"),
        ),
    ),
    # The second lacquer lot, a different vendor lot number, and inspection
    # waived on the day because it is a repeat of a shade already approved. It
    # goes straight to AVAILABLE, so the same item ends the stage sitting in two
    # stock states under two batches - which is exactly what the batch and
    # traceability screens exist to show.
    _Receipt(
        marker=("GRN", "LAC", "B"),
        order=("PO", "LACQUER"),
        received_in=-3,
        delivery_note="CE/DC/1206",
        invoice_number="CE/INV/1021",
        gate_entry="GE-1206",
        vehicle="GJ 03 CC 2277",
        transporter=None,
        notes="Balance of the order. Repeat of an approved shade; inspection waived.",
        lines=(
            _ReceiptLine(
                item="CN-LACQUER",
                received="120",
                accepted="120",
                batch_number="LQ-2612-B",
                manufactured_in=-20,
                expires_in=345,
                requires_qc=False,
            ),
        ),
    ),
)


# -----------------------------------------------------------------------------
# Purchase returns
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Return:
    marker: tuple[str, ...]
    receipt: tuple[str, ...]
    item: str
    quantity: str
    returned_in: int
    reason: str
    line_reason: str
    debit_note: str
    notes: str
    #: Posted returns move stock back out against the lot it came in on. The
    #: draft one does not, and must not.
    post: bool


RETURNS: tuple[_Return, ...] = (
    # Posted: 60kg of ingot accepted at the gate and only found porous once the
    # die caster ran the first melt. Material accepted and later found defective
    # goes back on a return, which posts the offsetting movement - never by
    # retracting the receipt that created the ledger entry.
    _Return(
        marker=("PRTN", "ZINC"),
        receipt=("GRN", "METALS"),
        item="RM-ZINC",
        quantity="60",
        returned_in=-24,
        reason="Porosity found on the first melt; replacement agreed with the supplier",
        line_reason="Gas porosity in the castings traced to this bundle",
        debit_note="DN/2609/011",
        notes="Supplier's works engineer inspected and accepted the claim.",
        post=True,
    ),
    # Still a draft. Raised, priced, not yet sent, so no stock has moved.
    _Return(
        marker=("PRTN", "SPRING"),
        receipt=("GRN", "COMP"),
        item="BO-SPRING",
        quantity="500",
        returned_in=-2,
        reason="Free length out of tolerance on the last carton",
        line_reason="Measured 17.9mm against a drawing minimum of 18.2mm",
        debit_note="DN/2612/003",
        notes="Awaiting the vendor's collection note before this is posted.",
        post=False,
    ),
)


# =============================================================================
# Requisitions
# =============================================================================


async def _seed_requisitions(ctx: DemoContext) -> None:
    """Raise the four requisitions and walk each one to the state it should rest in."""
    existing = await _existing(ctx, PurchaseRequisition)

    for spec in REQUISITIONS:
        marker = ctx.code(*spec.marker)
        requisition = existing.get(marker)
        if requisition is None:
            requisition = await _create_requisition(ctx, spec, marker)
            _seen(ctx, "requisition", marker, requisition.id, created=True)
        else:
            _seen(ctx, "requisition", marker, requisition.id, created=False)

        await _walk_requisition(ctx, requisition, spec.stop_at)


async def _create_requisition(
    ctx: DemoContext, spec: _Requisition, marker: str
) -> PurchaseRequisition:
    requisition, _ = await procurement.create_requisition(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        RequisitionCreate(
            requisition_date=_on(spec.raised_in),
            required_date=_on(spec.required_in),
            warehouse_id=ctx.id_of("warehouse", spec.warehouse),
            priority=spec.priority,
            purpose=spec.purpose,
            notes=_tagged(marker, spec.notes),
            # Raised by hand off the production plan. MRP-sourced demand is the
            # MRP stage's story to tell, and claiming it here would give the
            # duplicate matcher a source reference nothing actually produced.
            source_type=PurchaseSourceType.MANUAL,
            lines=[
                RequisitionLineIn(
                    line_number=index,
                    item_id=_item(ctx, line.item),
                    quantity=Decimal(line.quantity),
                    required_date=_on(spec.required_in),
                    estimated_unit_price=Decimal(line.estimated_unit_price),
                    suggested_vendor_id=(
                        _vendor(ctx, line.suggested_vendor) if line.suggested_vendor else None
                    ),
                    notes=line.notes,
                )
                for index, line in enumerate(spec.lines, start=1)
            ],
        ),
    )
    return requisition


async def _walk_requisition(
    ctx: DemoContext, requisition: PurchaseRequisition, stop: RequisitionStatus
) -> None:
    """Submit and approve along the state machine's own path, never by assignment.

    Written so it can be re-entered: a run that died between the two steps leaves
    a PENDING_APPROVAL requisition, and the next run finishes the job rather than
    tripping over it. A requisition already CONVERTED is not on this route and is
    left exactly as the order that converted it left it.
    """
    current = RequisitionStatus(requisition.status)
    if current not in _REQUISITION_ROUTE or stop not in _REQUISITION_ROUTE:
        return
    start, end = _REQUISITION_ROUTE.index(current), _REQUISITION_ROUTE.index(stop)
    for target in _REQUISITION_ROUTE[start + 1 : end + 1]:
        await procurement.transition_requisition(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            requisition.id,
            target,
            reason=_REQUISITION_REASON[target],
        )


# =============================================================================
# Requests for quotation
# =============================================================================


async def _seed_rfqs(ctx: DemoContext) -> None:
    """Raise both enquiries and send them. Closing waits until the answers are in."""
    existing = await _existing(ctx, Rfq)

    for spec in RFQS:
        marker = ctx.code(*spec.marker)
        rfq = existing.get(marker)
        if rfq is None:
            rfq = await _create_rfq(ctx, spec, marker)
            _seen(ctx, "rfq", marker, rfq.id, created=True)
        else:
            _seen(ctx, "rfq", marker, rfq.id, created=False)

        if RfqStatus(rfq.status) is RfqStatus.DRAFT:
            await procurement.transition_rfq(
                ctx.session,
                ctx.tenant,
                rfq.id,
                RfqStatus.SENT,
                reason="Enquiry e-mailed to the vendors named on it",
            )


async def _create_rfq(ctx: DemoContext, spec: _Rfq, marker: str) -> Rfq:
    # Raised from the requisition when there is one: the service copies the open
    # requisition lines across and stamps `requisition_line_id` on each of them,
    # which is what makes the requisition-to-order trace hold together later.
    requisition_id = (
        ctx.id_of("requisition", ctx.code(*spec.requisition)) if spec.requisition else None
    )
    lines = (
        [
            RfqLineIn(
                line_number=index,
                item_id=_item(ctx, line.item),
                quantity=Decimal(line.quantity),
                required_date=_on(spec.required_in),
                specification=line.specification,
            )
            for index, line in enumerate(spec.lines, start=1)
        ]
        if spec.lines
        else None
    )

    rfq, _ = await procurement.create_rfq(
        ctx.session,
        ctx.tenant,
        RfqCreate(
            warehouse_id=ctx.id_of("warehouse", "WH-RM"),
            rfq_date=_on(spec.raised_in),
            response_due_date=_on(spec.response_due_in),
            required_date=_on(spec.required_in),
            requisition_id=requisition_id,
            vendor_ids=[_vendor(ctx, code) for code in spec.vendors],
            currency="INR",
            terms=spec.terms,
            notes=_tagged(marker, spec.notes),
            lines=lines,
        ),
    )
    return rfq


# =============================================================================
# Quotations
# =============================================================================


async def _find_quotation(
    ctx: DemoContext, rfq_id: uuid.UUID, vendor_id: uuid.UUID
) -> VendorQuotation | None:
    """The idempotence key for a quotation is the pair the service itself enforces.

    ``create_quotation`` refuses a second offer from the same vendor on the same
    enquiry, so (RFQ, vendor) is the identity. A marker in the notes would be a
    second key that could disagree with the first one.
    """
    stmt = select(VendorQuotation).where(
        VendorQuotation.company_id == ctx.company_id,
        VendorQuotation.rfq_id == rfq_id,
        VendorQuotation.vendor_id == vendor_id,
    )
    return (await ctx.session.execute(stmt)).scalars().first()


async def _seed_quotations(ctx: DemoContext) -> None:
    """Record each vendor's answer, then the buyer's decision on it."""
    for spec in QUOTATIONS:
        marker = ctx.code(*spec.marker)
        rfq_id = ctx.id_of("rfq", ctx.code(*spec.rfq))
        vendor_id = _vendor(ctx, spec.vendor)

        quotation = await _find_quotation(ctx, rfq_id, vendor_id)
        if quotation is None:
            quotation = await _create_quotation(ctx, spec, rfq_id, vendor_id, marker)
            if quotation is None:
                continue
            _seen(ctx, "quotation", marker, quotation.id, created=True)
        else:
            _seen(ctx, "quotation", marker, quotation.id, created=False)

        # SELECTED and REJECTED are both terminal, so a second run must not
        # attempt the decision again.
        if spec.decision is None:
            continue
        if QuotationStatus(quotation.status) is not QuotationStatus.RECEIVED:
            continue
        await procurement.decide_quotation(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            quotation.id,
            spec.decision,
            reason=spec.decision_reason or "Decided on the comparison",
        )


async def _create_quotation(
    ctx: DemoContext, spec: _Quotation, rfq_id: uuid.UUID, vendor_id: uuid.UUID, marker: str
) -> VendorQuotation | None:
    """Price the vendor's answer against the lines the enquiry actually asked for.

    The quantities come from the RFQ rather than from this file, so a vendor is
    never recorded as having quoted for something nobody asked about - and the
    ``rfq_line_id`` on each line is what lets the comparison screen put two
    vendors' prices for one item side by side.
    """
    rfq = await procurement.get_rfq_detail(ctx.session, ctx.tenant, rfq_id)
    offers = {_item(ctx, offer.item): offer for offer in spec.offers}

    lines: list[QuotationLineIn] = []
    for index, rfq_line in enumerate(rfq.lines, start=1):
        offer = offers.get(rfq_line.item_id)
        if offer is None:
            continue
        lines.append(
            QuotationLineIn(
                line_number=index,
                item_id=rfq_line.item_id,
                quantity=rfq_line.quantity,
                unit_price=Decimal(offer.unit_price),
                discount_percentage=(
                    Decimal(offer.discount_percentage) if offer.discount_percentage else None
                ),
                rfq_line_id=rfq_line.id,
                lead_time_days=offer.lead_time_days,
                min_order_quantity=(
                    Decimal(offer.min_order_quantity) if offer.min_order_quantity else None
                ),
                notes=offer.notes,
            )
        )

    if not lines:
        # The enquiry has no line this vendor priced - which can only happen if
        # the requisition it was raised from changed shape. Degrade to no
        # quotation rather than inventing one for an item nobody asked about.
        ctx.skipped.append(
            (STAGE, f"{marker}: none of the quoted items are on {rfq.code}; quotation not recorded")
        )
        return None

    quotation, _ = await procurement.create_quotation(
        ctx.session,
        ctx.tenant,
        QuotationCreate(
            rfq_id=rfq_id,
            vendor_id=vendor_id,
            vendor_reference=spec.vendor_reference,
            quotation_date=_on(spec.quoted_in),
            valid_until=_on(spec.valid_until_in),
            currency="INR",
            payment_terms_days=spec.payment_terms_days,
            lead_time_days=spec.lead_time_days,
            freight_amount=Decimal(spec.freight),
            terms=spec.terms,
            notes=_tagged(marker, spec.notes),
            lines=lines,
        ),
    )
    return quotation


async def _close_answered_rfqs(ctx: DemoContext) -> None:
    """Close the enquiries whose decision has been taken.

    Separate from sending them, and run after the quotations exist, because a
    CLOSED enquiry can no longer take one - ``_load_rfq_for_quoting`` refuses it,
    correctly.
    """
    for spec in RFQS:
        if not spec.close_after_quotes:
            continue
        rfq_id = ctx.id_of("rfq", ctx.code(*spec.marker))
        rfq = await procurement.get_rfq_detail(ctx.session, ctx.tenant, rfq_id)
        if RfqStatus(rfq.status) is not RfqStatus.SENT:
            continue
        await procurement.transition_rfq(
            ctx.session,
            ctx.tenant,
            rfq_id,
            RfqStatus.CLOSED,
            reason="Offers compared and the order placed; enquiry closed",
        )


# =============================================================================
# Purchase orders
# =============================================================================


async def _seed_orders(ctx: DemoContext) -> None:
    """Raise the eight hand-typed orders and walk each to where it should rest."""
    existing = await _existing(ctx, PurchaseOrder)

    for spec in ORDERS:
        marker = ctx.code(*spec.marker)
        order = existing.get(marker)
        if order is None:
            lines = [
                PurchaseOrderLineIn(
                    line_number=index,
                    item_id=_item(ctx, line.item),
                    quantity=Decimal(line.quantity),
                    unit_price=Decimal(line.unit_price),
                    discount_percentage=(
                        Decimal(line.discount_percentage) if line.discount_percentage else None
                    ),
                    required_date=_on(line.required_in),
                    requires_qc=line.requires_qc,
                    notes=line.notes,
                )
                for index, line in enumerate(spec.lines, start=1)
            ]
            order = await _create_order(ctx, spec, marker, lines)
            if order is None:
                continue
            _seen(ctx, "purchase_order", marker, order.id, created=True)
        else:
            _seen(ctx, "purchase_order", marker, order.id, created=False)

        await _walk_order(ctx, order, spec.stop_at)
        await _record_shipment(ctx, spec, order.id)


async def _seed_sourced_order(ctx: DemoContext) -> None:
    """The order at the end of the quotation chain, priced from the offer that won.

    Its lines are read off the selected quotation rather than restated here, so
    the price on the order is the price that was quoted. Each line carries both
    the ``quotation_line_id`` it was priced from and the ``requisition_line_id``
    the demand originally came from, which is what pushes the ordered quantity
    back onto the requisition and moves it to CONVERTED.
    """
    marker = ctx.code(*SOURCED_ORDER.marker)
    existing = await _existing(ctx, PurchaseOrder)
    order = existing.get(marker)

    if order is None:
        quotation_id = ctx.maybe_id("quotation", ctx.code(*SOURCED_FROM))
        if quotation_id is None:
            ctx.skipped.append(
                (STAGE, f"{marker} not raised: the quotation it is priced from was not recorded")
            )
            return
        quotation = await procurement.get_quotation_detail(ctx.session, ctx.tenant, quotation_id)
        rfq = await procurement.get_rfq_detail(ctx.session, ctx.tenant, quotation.rfq_id)
        requisition_line_by_rfq_line = {line.id: line.requisition_line_id for line in rfq.lines}

        lines = [
            PurchaseOrderLineIn(
                line_number=index,
                item_id=line.item_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                discount_percentage=(
                    line.discount_percentage if line.discount_percentage > 0 else None
                ),
                required_date=_on(SOURCED_REQUIRED_IN),
                requisition_line_id=requisition_line_by_rfq_line.get(line.rfq_line_id)
                if line.rfq_line_id
                else None,
                quotation_line_id=line.id,
            )
            for index, line in enumerate(quotation.lines, start=1)
        ]
        order = await _create_order(
            ctx,
            SOURCED_ORDER,
            marker,
            lines,
            source_type=PurchaseSourceType.VENDOR_QUOTATION,
            source_id=quotation_id,
        )
        if order is None:
            return
        _seen(ctx, "purchase_order", marker, order.id, created=True)
    else:
        _seen(ctx, "purchase_order", marker, order.id, created=False)

    await _walk_order(ctx, order, SOURCED_ORDER.stop_at)
    await _record_shipment(ctx, SOURCED_ORDER, order.id)


async def _create_order(
    ctx: DemoContext,
    spec: _Order,
    marker: str,
    lines: Sequence[PurchaseOrderLineIn],
    *,
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL,
    source_id: uuid.UUID | None = None,
) -> PurchaseOrder | None:
    """Raise one order, or say plainly why it could not be raised.

    ``acknowledge_duplicates`` is deliberately never set. The delivery dates in
    this file are spread so that no two open orders put the same item on the same
    vendor and warehouse inside the seven-day window; if that ever stops being
    true the order is skipped with the refusal attached, because a demonstration
    database full of acknowledged duplicates teaches the wrong lesson about the
    rule the acknowledgement exists to override.
    """
    try:
        order, _, _ = await procurement.create_purchase_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            PurchaseOrderCreate(
                vendor_id=_vendor(ctx, spec.vendor),
                warehouse_id=ctx.id_of("warehouse", spec.warehouse),
                po_date=_on(spec.raised_in),
                expected_delivery_date=_on(spec.expected_in),
                priority=spec.priority,
                currency="INR",
                freight_amount=Decimal(spec.freight),
                delivery_address=spec.delivery_address,
                terms=spec.terms,
                notes=_tagged(marker, spec.notes),
                vendor_reference=spec.vendor_reference,
                source_type=source_type,
                source_id=source_id,
                lines=list(lines),
            ),
        )
    except BusinessRuleError as exc:
        # An unapproved vendor, an item somebody switched off, or a duplicate
        # this file's dates failed to keep apart. Costs one order and its
        # receipt; every other order still lands.
        ctx.skipped.append((STAGE, f"{marker} not raised: {exc}"))
        return None
    return order


async def _walk_order(ctx: DemoContext, order: PurchaseOrder, stop: PurchaseOrderStatus) -> None:
    """Submit, approve, send and acknowledge, one audited transition at a time.

    Re-entrant, and one-way: an order that a previous run already received
    against is past the end of this route, so the walk does nothing rather than
    trying to send an order that has already been delivered.
    """
    current = PurchaseOrderStatus(order.status)
    if current not in _ORDER_ROUTE or stop not in _ORDER_ROUTE:
        return
    start, end = _ORDER_ROUTE.index(current), _ORDER_ROUTE.index(stop)
    for target in _ORDER_ROUTE[start + 1 : end + 1]:
        await procurement.transition_purchase_order(
            ctx.session,
            ctx.tenant,
            ctx.principal,
            order.id,
            target,
            reason=_ORDER_REASON[target],
        )


async def _record_shipment(ctx: DemoContext, spec: _Order, order_id: uuid.UUID) -> None:
    """Attach the inbound courier details a vendor would post from the portal.

    Guarded on the courier already being absent rather than simply re-writing the
    same values: an unconditional call writes an audit row on every re-run, and
    an audit trail that grows without anything having changed is noise.
    """
    if spec.shipment is None:
        return
    order = await procurement.get_purchase_order_detail(ctx.session, ctx.tenant, order_id)
    if order.courier_name:
        return
    courier, tracking, days_ago = spec.shipment
    try:
        await procurement.record_shipment(
            ctx.session,
            ctx.tenant,
            order_id,
            courier_name=courier,
            tracking_reference=tracking,
            shipped_at=utcnow() - timedelta(days=days_ago),
        )
    except BusinessRuleError as exc:
        ctx.skipped.append((STAGE, f"shipment details not recorded on {order.code}: {exc}"))


# =============================================================================
# Goods receipts - where stock enters
# =============================================================================


async def _seed_receipts(ctx: DemoContext) -> None:
    """Book every delivery in and post it, in the order it physically arrived."""
    existing = await _existing(ctx, GoodsReceipt)

    for spec in RECEIPTS:
        marker = ctx.code(*spec.marker)
        order_code = ctx.code(*spec.order)
        order_id = ctx.maybe_id("purchase_order", order_code)
        if order_id is None:
            ctx.skipped.append((STAGE, f"{marker} not booked: {order_code} was not raised"))
            continue

        receipt = existing.get(marker)
        if receipt is None:
            receipt = await _create_receipt(ctx, spec, marker, order_id)
            if receipt is None:
                continue
            _seen(ctx, "goods_receipt", marker, receipt.id, created=True)
        else:
            _seen(ctx, "goods_receipt", marker, receipt.id, created=False)

        # POSTED is terminal, so this only ever runs once - and it is separate
        # from creation so a run that died between the two finishes the job
        # rather than leaving a delivery booked in but never taken into stock.
        if GoodsReceiptStatus(receipt.status) is not GoodsReceiptStatus.DRAFT:
            continue
        try:
            await procurement.post_goods_receipt(ctx.session, ctx.tenant, ctx.principal, receipt.id)
        except BusinessRuleError as exc:
            ctx.skipped.append((STAGE, f"{marker} booked but not posted: {exc}"))


async def _create_receipt(
    ctx: DemoContext, spec: _Receipt, marker: str, order_id: uuid.UUID
) -> GoodsReceipt | None:
    """Key one delivery note against the order lines it answers.

    Receipt lines are matched to order lines by item, because that is how a
    storekeeper reads a delivery note - the line numbers on the vendor's document
    are their own, not ours.
    """
    order = await procurement.get_purchase_order_detail(ctx.session, ctx.tenant, order_id)
    order_line_by_item = {line.item_id: line.id for line in order.lines}

    lines: list[GoodsReceiptLineIn] = []
    for index, line in enumerate(spec.lines, start=1):
        item_id = _item(ctx, line.item)
        order_line_id = order_line_by_item.get(item_id)
        if order_line_id is None:
            ctx.skipped.append(
                (STAGE, f"{marker} line {index}: {line.item} is not on {order.code}; line dropped")
            )
            continue
        lines.append(
            GoodsReceiptLineIn(
                line_number=index,
                purchase_order_line_id=order_line_id,
                received_quantity=Decimal(line.received),
                accepted_quantity=Decimal(line.accepted),
                rejected_quantity=Decimal(line.rejected),
                rejection_reason=line.rejection_reason,
                batch_number=line.batch_number,
                manufactured_on=(
                    _on(line.manufactured_in) if line.manufactured_in is not None else None
                ),
                expires_on=_on(line.expires_in) if line.expires_in is not None else None,
                # Left unset on purpose: `BalanceGrain` includes `location_id`,
                # so stock received into a bin can only be issued from that bin,
                # and choosing bins here would commit every later stage to
                # knowing which ones. See the module docstring.
                location_id=None,
                requires_qc=line.requires_qc,
                notes=line.notes,
            )
        )

    if not lines:
        ctx.skipped.append((STAGE, f"{marker} not booked: no line matched {order.code}"))
        return None

    try:
        receipt, _ = await procurement.create_goods_receipt(
            ctx.session,
            ctx.tenant,
            GoodsReceiptCreate(
                purchase_order_id=order_id,
                receipt_date=_on(spec.received_in),
                vendor_delivery_note=spec.delivery_note,
                vendor_invoice_number=spec.invoice_number,
                gate_entry_number=spec.gate_entry,
                vehicle_number=spec.vehicle,
                transporter_id=(
                    ctx.maybe_id("transporter", spec.transporter) if spec.transporter else None
                ),
                notes=_tagged(marker, spec.notes),
                lines=lines,
            ),
        )
    except (BusinessRuleError, ValidationError) as exc:
        ctx.skipped.append((STAGE, f"{marker} not booked against {order.code}: {exc}"))
        return None
    return receipt


async def _short_close_cylinder_order(ctx: DemoContext) -> None:
    """Stop expecting the 2 750 cylinders the vendor withdrew.

    This is the whole mechanism behind the shortage the reorder report and MRP
    are supposed to show. Leaving the order PARTIALLY_RECEIVED would leave 2 750
    counted as incoming supply, MRP would net the shortage away against it, and
    the demonstration would quietly have no shortage in it at all. Short-closing
    is an audited decision to stop expecting the balance - the state machine has
    the edge precisely so this does not have to be a cancellation of an order
    that has already delivered against the ledger.
    """
    order_id = ctx.maybe_id("purchase_order", ctx.code("PO", "CYL"))
    if order_id is None:
        return
    order = await procurement.get_purchase_order_detail(ctx.session, ctx.tenant, order_id)
    if PurchaseOrderStatus(order.status) is not PurchaseOrderStatus.PARTIALLY_RECEIVED:
        return
    await procurement.transition_purchase_order(
        ctx.session,
        ctx.tenant,
        ctx.principal,
        order_id,
        PurchaseOrderStatus.CLOSED,
        reason="Vendor withdrew the balance - brass pin stock exhausted; re-sourcing",
    )


# =============================================================================
# Purchase returns
# =============================================================================


async def _seed_returns(ctx: DemoContext) -> None:
    """One return posted against the lot it came in on, and one left as a draft."""
    existing = await _existing(ctx, PurchaseReturn)

    for spec in RETURNS:
        marker = ctx.code(*spec.marker)
        receipt_code = ctx.code(*spec.receipt)
        receipt_id = ctx.maybe_id("goods_receipt", receipt_code)
        if receipt_id is None:
            ctx.skipped.append((STAGE, f"{marker} not raised: {receipt_code} was not posted"))
            continue

        purchase_return = existing.get(marker)
        if purchase_return is None:
            purchase_return = await _create_return(ctx, spec, marker, receipt_id)
            if purchase_return is None:
                continue
            _seen(ctx, "purchase_return", marker, purchase_return.id, created=True)
        else:
            _seen(ctx, "purchase_return", marker, purchase_return.id, created=False)

        if not spec.post:
            continue
        if PurchaseReturnStatus(purchase_return.status) is not PurchaseReturnStatus.DRAFT:
            continue
        try:
            await procurement.post_purchase_return(
                ctx.session, ctx.tenant, ctx.principal, purchase_return.id
            )
        except BusinessRuleError as exc:
            ctx.skipped.append((STAGE, f"{marker} raised but not posted: {exc}"))


async def _create_return(
    ctx: DemoContext, spec: _Return, marker: str, receipt_id: uuid.UUID
) -> PurchaseReturn | None:
    receipt = await procurement.get_goods_receipt_detail(ctx.session, ctx.tenant, receipt_id)
    item_id = _item(ctx, spec.item)
    receipt_line = next((line for line in receipt.lines if line.item_id == item_id), None)
    if receipt_line is None:
        ctx.skipped.append((STAGE, f"{marker} not raised: {spec.item} is not on {receipt.code}"))
        return None

    try:
        purchase_return, _ = await procurement.create_purchase_return(
            ctx.session,
            ctx.tenant,
            PurchaseReturnCreate(
                goods_receipt_id=receipt_id,
                reason=spec.reason,
                return_date=_on(spec.returned_in),
                debit_note_number=spec.debit_note,
                notes=_tagged(marker, spec.notes),
                lines=[
                    PurchaseReturnLineIn(
                        line_number=1,
                        goods_receipt_line_id=receipt_line.id,
                        quantity=Decimal(spec.quantity),
                        reason=spec.line_reason,
                    )
                ],
            ),
        )
    except BusinessRuleError as exc:
        ctx.skipped.append((STAGE, f"{marker} not raised against {receipt.code}: {exc}"))
        return None
    return purchase_return


# =============================================================================
# The serial-tracked gap
# =============================================================================


async def _note_serial_gap(ctx: DemoContext) -> None:
    """Record, in the run report, why no serialised stock arrives through buying.

    Not a silent omission and not a workaround. Two independent rules make a
    serialised goods receipt impossible, and both of them are correct:

    * ``procurement.service`` refuses a serial-tracked item on a receipt line -
      the line has nowhere to carry the many serials that arrive against it, and
      the inventory engine will not move such an item without one;
    * the demonstration's only serial-tracked item is a manufactured finished
      good, so ``can_purchase`` is false and it could not be ordered anyway.

    Serialised units are born in inventory, which is where they should be
    registered. The list is read from the item master rather than named as a
    constant here, so the note stops appearing the day the receipt contract grows
    somewhere to put a serial number - and names the right items if the item
    master changes shape first.
    """
    stmt = select(Item.code).where(
        Item.company_id == ctx.company_id,
        Item.is_serial_tracked.is_(True),
        Item.deleted_at.is_(None),
    )
    codes = sorted((await ctx.session.execute(stmt)).scalars())
    if not codes:
        return
    ctx.skipped.append(
        (
            STAGE,
            f"no serialised receipt for {', '.join(codes)}: a goods receipt line has "
            "nowhere to record the serials that arrive against it, so procurement refuses "
            "a serial-tracked item outright - register their units through inventory",
        )
    )


# =============================================================================
# Entry point
# =============================================================================


async def seed(ctx: DemoContext) -> None:
    """The buying pipeline, in the order it actually happens.

    The order is not decoration. An RFQ needs an approved requisition to copy; a
    quotation needs an enquiry that is still open, so the enquiry is closed only
    after the answers are in; the order at the end of the chain needs the
    quotation that won; a receipt needs an order the vendor already has; a return
    needs a receipt that has actually posted; and the cylinder order can only be
    short-closed once its one token delivery has been taken into stock.
    """
    await _seed_requisitions(ctx)
    await _seed_rfqs(ctx)
    await _seed_quotations(ctx)
    await _close_answered_rfqs(ctx)

    # The quotation-sourced order first, so the requisition it converts is
    # marked ORDERED before anything else looks at the demand.
    await _seed_sourced_order(ctx)
    await _seed_orders(ctx)

    await _seed_receipts(ctx)
    await _short_close_cylinder_order(ctx)
    await _seed_returns(ctx)

    await _note_serial_gap(ctx)
