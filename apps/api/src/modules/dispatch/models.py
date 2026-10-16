"""Dispatches, their lines and their tracking history.

Master rules sections 22 and 23. Four decisions shape every table below.

**A dispatch is the moment goods stop being ours.** ``DISPATCHED`` is the only
status in the lifecycle that moves stock: it consumes the reservation the sales
order was holding and posts the ``DISPATCH`` ledger rows. Everything before it
is planning and everything after it is reporting on a consignment that has
already left the building. That is why ``dispatch_lines`` carries
``reservation_id`` and ``inventory_transaction_id`` - a line that shipped must
be able to name the promise it drew on and the ledger row it wrote, months
later, without re-deriving either.

**The tracking history is append-only.** ``DispatchTrackingEvent`` is an
:class:`~src.core.models.ImmutableLedgerEntity` on purpose: a delivery history
somebody can edit answers no question worth asking. "The courier said it was in
Pune on Tuesday" is either a fact that was recorded or it is not; a correction is
a new event, and the pair is the truth. Same argument as
``inventory_transactions``, applied to a different ledger.

**Delay is derived, never stored as a flag.** A consignment is late when its
``expected_delivery_date`` has passed and it has not been delivered. An
``is_delayed`` boolean would need something to keep it true as the clock moves,
and would be wrong every night between midnight and whenever that something ran.
The partial index ``ix_dispatches_in_flight_expected`` is what makes the derived
question cheap. ``delay_notified_at`` is *not* the flag - it records that the
hourly sweep has already told somebody, so a two-week delay does not produce 336
alerts.

**``customer_id`` and ``vendor_id`` are both nullable, and which one is required
depends on the type.** A SALES dispatch goes to a customer; a PURCHASE_RETURN or
a JOB_WORK consignment goes to a vendor; a TRANSFER goes to another of our own
warehouses and has neither. CHECK constraints, not application politeness,
enforce the pairing - a consignment addressed to nobody is the defect that puts
goods on the wrong lorry.

**Serial and carton sets are arrays on the line, not child tables.** Same
reasoning as ``carton_contents.serial_ids``: the set is small, it is always read
with its line, and it is written once when the line ships. The GIN indexes are
what make "which dispatch carried serial X" a lookup rather than a scan, which is
the whole point of recording them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import ImmutableLedgerEntity, OptimisticLockMixin, TenantEntity, sql_in_list
from src.core.types import GUID, Money, Quantity

#: Lines on one consignment. More distinct items than this is a container
#: manifest or a data error, and the dispatch path locks every line's balance row
#: inside one transaction - so the set has to be bounded.
MAX_LINES_PER_DISPATCH = 200

#: Serialised units named on one dispatch line: one serial per unit shipped.
#: A line beyond this is a bulk movement that should be split across lines.
MAX_SERIALS_PER_LINE = 10_000

#: Cartons named on one dispatch line. Bounded by ``MAX_CARTONS_PER_ORDER`` in
#: the packing module, with room for a consignment collecting several orders.
MAX_CARTONS_PER_LINE = 5_000

#: ``eway_bill_number`` width. An Indian e-way bill number is 12 digits; the
#: extra room absorbs a formatted or prefixed value without truncating a
#: statutory reference.
EWAY_BILL_LENGTH = 24


class DispatchType(StrEnum):
    """What kind of consignment this is.

    Not a cosmetic label: it decides which counterparty column is mandatory and
    whether a sales order may be attached at all (see the CHECK constraints).
    """

    #: Finished goods leaving against a customer's order.
    SALES = "SALES"
    #: Rejected or surplus material going back to the vendor who supplied it.
    PURCHASE_RETURN = "PURCHASE_RETURN"
    #: Material going out to a job-work vendor for an operation.
    JOB_WORK = "JOB_WORK"
    #: Stock moving between our own locations.
    TRANSFER = "TRANSFER"


class DispatchStatus(StrEnum):
    """Lifecycle of a consignment (database.md section 7).

    The tail of this list - ``QC_PENDING`` onwards - describes what happens to
    the consignment *at the far end*: the receiver inspects it and books it in.
    Those statuses are reported to us (a customer's acknowledgement, a vendor
    portal, a courier feed) rather than performed by us, which is why every one
    of them is reached by recording a tracking event.
    """

    NOT_DISPATCHED = "NOT_DISPATCHED"
    READY_FOR_DISPATCH = "READY_FOR_DISPATCH"
    DISPATCHED = "DISPATCHED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    QC_PENDING = "QC_PENDING"
    RECEIVED = "RECEIVED"
    QC_COMPLETED = "QC_COMPLETED"
    GRN_COMPLETED = "GRN_COMPLETED"
    #: ASSUMPTION: database.md section 7 lists no cancelled state for a dispatch.
    #: A consignment planned and then called off before it leaves still needs a
    #: way out, and deleting the row would lose the document number and the trail
    #: of why it was planned. Cancellation is therefore permitted only from the
    #: two statuses in which nothing has moved - see ``service.DISPATCH_LIFECYCLE``.
    CANCELLED = "CANCELLED"


class FreightPaidBy(StrEnum):
    """Who bears the freight on this consignment.

    ``CONSIGNOR`` is us, the sender; ``CONSIGNEE`` is the receiver. Captured per
    consignment rather than inferred from the customer's standing terms: the
    terms are a default, this is what was actually agreed for this lorry, and it
    decides whether the freight amount is recoverable.
    """

    CONSIGNOR = "CONSIGNOR"
    CONSIGNEE = "CONSIGNEE"


class TrackingEventSource(StrEnum):
    """Where a tracking event came from.

    Recorded because the three sources differ in trustworthiness. A ``MANUAL``
    event is somebody's typing; a ``COURIER_API`` event is the carrier's own
    scan. When they disagree, the source is what lets a human decide which to
    believe.
    """

    MANUAL = "MANUAL"
    COURIER_API = "COURIER_API"
    VENDOR_PORTAL = "VENDOR_PORTAL"


#: Statuses in which the consignment is on the road: gone, not yet arrived. The
#: delay sweep and the partial index below read exactly this set, so the tuple
#: and the index have to change together.
IN_FLIGHT_STATUSES: tuple[DispatchStatus, ...] = (
    DispatchStatus.DISPATCHED,
    DispatchStatus.IN_TRANSIT,
)

#: Statuses that mean the goods reached the receiver. Past any of these the
#: consignment can no longer be late, whatever the expected date said.
DELIVERED_STATUSES: tuple[DispatchStatus, ...] = (
    DispatchStatus.DELIVERED,
    DispatchStatus.QC_PENDING,
    DispatchStatus.RECEIVED,
    DispatchStatus.QC_COMPLETED,
    DispatchStatus.GRN_COMPLETED,
)

#: Statuses in which the header and its lines may still be edited. Once
#: DISPATCHED, stock has moved against these lines and the document is evidence
#: rather than a plan.
EDITABLE_STATUSES: tuple[DispatchStatus, ...] = (
    DispatchStatus.NOT_DISPATCHED,
    DispatchStatus.READY_FOR_DISPATCH,
)


class Dispatch(TenantEntity, OptimisticLockMixin):
    """One consignment: what is going out, to whom, on whose lorry."""

    __tablename__ = "dispatches"

    # --- identity -----------------------------------------------------------
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_type: Mapped[DispatchType] = mapped_column(
        String(24),
        nullable=False,
        default=DispatchType.SALES,
        server_default=DispatchType.SALES.value,
    )

    # --- what this consignment fulfils --------------------------------------
    #: The demand behind the consignment. RESTRICT: the fulfilment arithmetic on
    #: the order's lines was advanced by this dispatch and the two have to stay
    #: reconcilable, so an order with a dispatch against it cannot be deleted.
    sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: The boxes. Nullable because a bulk consignment - loose material to a
    #: job-work vendor, a stock transfer - is loaded without a packing order.
    packing_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("packing_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # --- counterparty -------------------------------------------------------
    # Which of these is required depends on ``dispatch_type``; see the CHECK
    # constraints. A TRANSFER legitimately has neither.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: ASSUMPTION: the contract's column list names ``customer_id`` only, but the
    #: tracking board in master rules section 23 has a "vendor / customer" column
    #: and two of the four dispatch types go to a vendor. Without this the
    #: counterparty on a purchase return or a job-work challan cannot be named.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    #: Where the goods physically leave from. NOT NULL: every ``DISPATCH``
    #: movement has to name the warehouse it decrements.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- lifecycle ----------------------------------------------------------
    status: Mapped[DispatchStatus] = mapped_column(
        String(24),
        nullable=False,
        default=DispatchStatus.NOT_DISPATCHED,
        server_default=DispatchStatus.NOT_DISPATCHED.value,
    )

    # --- dates ---------------------------------------------------------------
    #: The commercial date of the consignment - what the lorry receipt and the
    #: invoice carry. A date, not a timestamp: two consignments booked on the
    #: same day must not sort differently because of a spurious clock reading.
    dispatch_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The instant the goods actually left, stamped by the dispatch action.
    #: Distinct from ``dispatch_date``, which is planned when the consignment is
    #: raised: "we said Tuesday, it went out Wednesday 19:40" is a real question
    #: and one column cannot answer it.
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: What we promised the receiver. Nullable: a walk-in collection has no
    #: delivery commitment, and the delay sweep must not invent one.
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- transport (master rules section 23 capture list) --------------------
    #: The carrier from the transporter master. Nullable because a customer's own
    #: vehicle collecting at the gate is a real dispatch with no transporter row
    #: behind it - which is exactly why ``transport_name`` exists too.
    transporter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("transporters.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: The courier brand as it appears on the consignment note. Kept beside
    #: ``transporter_id`` rather than derived from it: a master record renamed
    #: two years later must not restate what an old lorry receipt said.
    courier_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: The transport company on the paperwork when it is not one of our
    #: registered transporters - a customer's own vehicle, a hired lorry.
    transport_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: The carrier's own consignment reference (AWB, docket). What a customer
    #: quotes when they ring to ask where their goods are, so it is indexed.
    tracking_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: Lorry receipt number - the road-transport equivalent of an AWB.
    lr_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    driver_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    driver_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- consignment measurements -------------------------------------------
    # Frozen onto the header rather than aggregated from the packing order on
    # read: they are what the transporter quotes and bills against, and a later
    # carton correction must not retroactively restate a figure already invoiced.
    total_boxes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_weight_kg: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    freight_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    freight_paid_by: Mapped[FreightPaidBy | None] = mapped_column(String(16), nullable=True)

    # --- statutory -----------------------------------------------------------
    #: Mandatory in India above a value threshold. Deliberately not validated
    #: against that threshold here: the rule is tax configuration, and a hard
    #: block would stop a legitimate below-threshold consignment leaving.
    eway_bill_number: Mapped[str | None] = mapped_column(String(EWAY_BILL_LENGTH), nullable=True)
    #: The invoice travelling with the goods. SET NULL rather than RESTRICT: a
    #: document may be superseded, and losing the attachment must not make the
    #: dispatch record unreadable.
    invoice_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    # --- delivery evidence ---------------------------------------------------
    #: Who confirmed the delivery on our side. Distinct from ``updated_by_id``,
    #: which any later edit overwrites; this is written once and answers "who
    #: says these goods arrived".
    delivery_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: What the receiver signed, and any shortage or damage they noted.
    delivery_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Why the consignment missed its expected date. Captured on the document
    #: because a delay report is only useful if the explanation travels with the
    #: row rather than living in somebody's mailbox.
    delay_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When the delay sweep last raised an alert for this consignment. Not the
    #: delay flag - see the module docstring - purely the flood control that stops
    #: an hourly job re-notifying the same late lorry all week.
    delay_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_dispatches_company_id_code"),
        # The dispatch desk's default list: consignments by status and date.
        Index("ix_dispatches_company_status_date", "company_id", "status", "dispatch_date"),
        Index("ix_dispatches_company_customer", "company_id", "customer_id", "dispatch_date"),
        Index("ix_dispatches_company_vendor", "company_id", "vendor_id", "dispatch_date"),
        Index("ix_dispatches_company_warehouse_status", "company_id", "warehouse_id", "status"),
        # "Where is AWB 12345?" - the most common support question, and the
        # reason this is an index rather than a scan.
        Index(
            "ix_dispatches_tracking_id",
            "company_id",
            "tracking_id",
            postgresql_where=text("tracking_id IS NOT NULL"),
        ),
        # THE index behind the delay sweep and the delayed-consignment board.
        # Partial, because a mature company's table is mostly delivered rows the
        # sweep never looks at. Its predicate mirrors ``IN_FLIGHT_STATUSES``
        # exactly - the two have to change together.
        Index(
            "ix_dispatches_in_flight_expected",
            "company_id",
            "expected_delivery_date",
            postgresql_where=text(
                f"status IN ({sql_in_list(IN_FLIGHT_STATUSES)}) "
                "AND expected_delivery_date IS NOT NULL"
            ),
        ),
        CheckConstraint(f"status IN ({sql_in_list(DispatchStatus)})", name="status_is_known"),
        CheckConstraint(
            f"dispatch_type IN ({sql_in_list(DispatchType)})", name="dispatch_type_is_known"
        ),
        CheckConstraint(
            f"freight_paid_by IS NULL OR freight_paid_by IN ({sql_in_list(FreightPaidBy)})",
            name="freight_paid_by_is_known",
        ),
        CheckConstraint("total_boxes >= 0", name="total_boxes_non_negative"),
        CheckConstraint("total_weight_kg >= 0", name="total_weight_non_negative"),
        CheckConstraint("freight_amount >= 0", name="freight_amount_non_negative"),
        # A consignment cannot be promised or delivered before it leaves.
        CheckConstraint(
            "expected_delivery_date IS NULL OR expected_delivery_date >= dispatch_date",
            name="expected_delivery_not_before_dispatch",
        ),
        CheckConstraint(
            "actual_delivery_date IS NULL OR actual_delivery_date >= dispatch_date",
            name="actual_delivery_not_before_dispatch",
        ),
        # A consignment the receiver has taken must say when. Without this the
        # on-time-delivery figure is computed over rows that silently have no
        # arrival date, which reads as 100% on time.
        CheckConstraint(
            f"status NOT IN ({sql_in_list(DELIVERED_STATUSES)}) "
            "OR actual_delivery_date IS NOT NULL",
            name="delivered_dispatch_has_arrival_date",
        ),
        # Goods have to be addressed to somebody. Which somebody depends on the
        # type, and getting this wrong puts a consignment on the wrong lorry.
        CheckConstraint(
            "dispatch_type <> 'SALES' OR customer_id IS NOT NULL",
            name="sales_dispatch_names_a_customer",
        ),
        CheckConstraint(
            "dispatch_type NOT IN ('PURCHASE_RETURN', 'JOB_WORK') OR vendor_id IS NOT NULL",
            name="vendor_dispatch_names_a_vendor",
        ),
        # A sales order can only be fulfilled by a sales dispatch. Attaching one
        # to a purchase return would advance a customer's fulfilment against
        # goods travelling back to a supplier.
        CheckConstraint(
            "sales_order_id IS NULL OR dispatch_type = 'SALES'",
            name="sales_order_only_on_sales_dispatch",
        ),
    )


class DispatchLine(TenantEntity):
    """One item on a consignment, and the promise and ledger row behind it."""

    __tablename__ = "dispatch_lines"

    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("dispatches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Stable identity within the consignment. Printed on the delivery challan
    #: and quoted in a shortage claim, so it is a real number rather than an
    #: array index that shifts when a line is removed.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The order line this quantity is fulfilling. Nullable: a replacement part
    #: or a sample travels on the same lorry without a demand row behind it.
    #: RESTRICT because this row advanced the line's ``dispatched_quantity`` and
    #: the two have to stay reconcilable.
    sales_order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("sales_order_lines.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    returned_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: The exact serialised units that left. Given a warranty claim on a serial,
    #: this names the consignment, the date and the customer.
    serial_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, server_default=text("'{}'::uuid[]"), default=list
    )
    #: The cartons this line's goods travelled in. An array rather than a join
    #: table for the same reason as ``serial_ids``, and no foreign key because
    #: ``cartons`` belongs to the packing module and a bulk consignment
    #: legitimately has none.
    carton_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, server_default=text("'{}'::uuid[]"), default=list
    )

    # --- what the dispatch actually did -------------------------------------
    #: The ``stock_reservations`` row this line drew down. Bare UUID, no foreign
    #: key: ``allocation`` owns that table and the record of what shipped must
    #: outlive any housekeeping that tidies consumed reservations away.
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: The ``inventory_transactions`` row this line posted. Bare UUID for the
    #: same reason the ledger's own ``reference_id`` is one - the ledger outlives
    #: its source, and a foreign key here would invert that.
    inventory_transaction_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "dispatch_id", "line_number", name="uq_dispatch_lines_dispatch_line_number"
        ),
        Index("ix_dispatch_lines_dispatch", "company_id", "dispatch_id", "line_number"),
        Index("ix_dispatch_lines_item", "company_id", "item_id"),
        Index("ix_dispatch_lines_sales_order_line", "company_id", "sales_order_line_id"),
        # Traceability in the other direction: given a serial or a carton, which
        # consignment carried it? A btree cannot answer containment on an array.
        Index("ix_dispatch_lines_serials", "serial_ids", postgresql_using="gin"),
        Index("ix_dispatch_lines_cartons", "carton_ids", postgresql_using="gin"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "returned_quantity >= 0 AND returned_quantity <= quantity",
            name="returned_quantity_bounded",
        ),
        CheckConstraint(
            f"cardinality(serial_ids) <= {MAX_SERIALS_PER_LINE}", name="serial_count_bounded"
        ),
        CheckConstraint(
            f"cardinality(carton_ids) <= {MAX_CARTONS_PER_LINE}", name="carton_count_bounded"
        ),
        # A serialised line names one unit per serial. Anything else means the
        # array fell behind the quantity, which nobody notices until a recall.
        CheckConstraint(
            "cardinality(serial_ids) = 0 OR cardinality(serial_ids) = quantity",
            name="serial_count_matches_quantity",
        ),
    )


class DispatchTrackingEvent(ImmutableLedgerEntity):
    """One reported position or status of a consignment. Append-only.

    Never updated and never deleted: a tracking history somebody can edit is
    worth nothing, because the question it exists to answer - "what did we know,
    and when did we know it?" - can only be answered by rows that cannot move. A
    correction is a later event, and the pair is the record.
    """

    __tablename__ = "dispatch_tracking_events"

    dispatch_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("dispatches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The consignment status this event reports. Same vocabulary as
    #: ``dispatches.status``: an event either restates the current status with a
    #: new location, or it is the thing that moved it.
    event_status: Mapped[DispatchStatus] = mapped_column(String(24), nullable=False)
    #: When the event happened according to its source, which is not when we
    #: recorded it. A courier feed pulled at 09:00 reports a scan from 21:40 the
    #: previous evening, and sorting the history by ``created_at`` would show
    #: that scan after everything typed in this morning.
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Who reported it. Nullable and distinct from ``created_by_id``: a courier
    #: feed is written by a background job with no user behind it, and pretending
    #: otherwise would attribute a machine's scan to a person.
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[TrackingEventSource] = mapped_column(
        String(16),
        nullable=False,
        default=TrackingEventSource.MANUAL,
        server_default=TrackingEventSource.MANUAL.value,
    )

    __table_args__ = (
        # The history of one consignment - the tracking pane.
        Index("ix_dispatch_tracking_events_dispatch", "company_id", "dispatch_id", "event_time"),
        # Keyset paging over the whole feed (section 31). ``id`` is UUIDv7, so
        # the pair is a total order even when a batch import shares a timestamp.
        Index("ix_dispatch_tracking_events_keyset", "company_id", "created_at", "id"),
        CheckConstraint(
            f"event_status IN ({sql_in_list(DispatchStatus)})", name="event_status_is_known"
        ),
        CheckConstraint(f"source IN ({sql_in_list(TrackingEventSource)})", name="source_is_known"),
    )


__all__ = [
    "DELIVERED_STATUSES",
    "EDITABLE_STATUSES",
    "EWAY_BILL_LENGTH",
    "IN_FLIGHT_STATUSES",
    "MAX_CARTONS_PER_LINE",
    "MAX_LINES_PER_DISPATCH",
    "MAX_SERIALS_PER_LINE",
    "Dispatch",
    "DispatchLine",
    "DispatchStatus",
    "DispatchTrackingEvent",
    "DispatchType",
    "FreightPaidBy",
    "TrackingEventSource",
]
