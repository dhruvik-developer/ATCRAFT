"""Job work: material sent out for processing, and what comes back.

Master rules section 20. Raw handles go to a plating shop for PVD, castings go
to a machinist, finished parts go out for laser marking. Six modelling decisions
carry the weight of this file.

**THE RULE THIS MODULE EXISTS FOR: material at a vendor is still ours.**
Issuing posts ``JOB_WORK_ISSUE``, which moves the quantity out of
``on_hand_quantity`` and into ``inventory_balances.job_work_quantity`` - it does
**not** dispatch it, return it, or adjust it away. The goods are company
property sitting on somebody else's floor: they stay on the books, they stay in
stock reports and in the valuation, and they stay traceable to the vendor, the
challan and the order that sent them. They are simply not *available* to be
promised to anyone. Posting a job-work issue as a ``DISPATCH`` or an
``ADJUSTMENT_DECREASE`` is the mistake this module exists to prevent: the stock
would vanish from the balance sheet the moment it left the gate, and the only
record that it was ever ours would be a challan in a drawer. Every quantity in
``job_work_order_lines`` between ``issued_quantity`` and ``received_quantity``
has a matching quantity in that ``job_work_quantity`` pool, and
``service.reconciliation`` is the report that proves it.

**The output may be a different item.** A raw handle goes out, a PVD-coated
handle comes back, and they are different item codes with different costs and
different sale prices. ``expected_output_item_id`` is therefore its own column
rather than an assumption that what was sent is what returns. The input is drawn
down from the job-work pool and the output is received as a separate item; the
two are tied together by ``expected_output_quantity / quantity_to_issue``, the
yield ratio frozen when the order is raised.

**The challan is a statutory document, not a convenience.** Goods sent for job
work move under a delivery challan (GST rules 45 and 55): it names the parties,
the goods, their taxable value and the vehicle, and the return has to be
reconciled against it. It gets its own table, its own document number series and
``ondelete="RESTRICT"`` on the order, because a challan that can disappear with
its parent is not a record of anything.

**Receipts are separate rows, not columns on the order.** A vendor returns in
several lots, and each lot is a distinct event with its own date, its own vendor
challan number, its own charge and possibly its own inspection. Accumulating
them into the header would lose the arrival dates that make a vendor's
performance measurable and would make an over-receipt impossible to attribute.

**Charges accrue on receipt, not on issue.** ``estimated_charges`` is what the
order is expected to cost, frozen at approval from the agreed rate.
``total_charges`` is the sum of what the receipts actually accrued, and it is
what ``service.job_work_charges_for_period`` hands to finance. A vendor is paid
for good pieces delivered, so a short delivery costs less without anybody
raising a credit note.

**``qc_inspection_id`` is a bare UUID with no foreign key.** ``quality`` is
deployed separately from this module and a hard reference would make this table
uncreatable until it lands. ``qc_held_at`` records the hold independently, so
"this receipt is held for inspection" stays true and queryable whether or not an
inspection document exists yet - the same reasoning as the inventory ledger's
``reference_id`` (database.md section 4.2).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import OptimisticLockMixin, TenantEntity
from src.core.types import GUID, Money, Percentage, Quantity, UnitPrice

#: An order with more lines than this is a bulk import, not a consignment a
#: plating shop processes. The bound matters because issuing and receiving post
#: every line in one locked transaction.
MAX_LINES_PER_ORDER = 200

#: Free-text length for the vehicle registration printed on the challan.
VEHICLE_NUMBER_LENGTH = 24


class JobWorkProcess(StrEnum):
    """What the vendor does to the material.

    A classification of the service bought, in the vocabulary master rules
    section 20 names. No rule reads it to decide whether an operation is legal -
    that is what the item's ``can_job_work`` capability flag is for (database.md
    section 3). It drives the vendor picker, the rate card and the process-wise
    spend report.
    """

    POLISHING = "POLISHING"
    COATING = "COATING"
    PVD = "PVD"
    LASER_MARKING = "LASER_MARKING"
    MACHINING = "MACHINING"
    OTHER = "OTHER"


class RateBasis(StrEnum):
    """How the vendor's charge is computed.

    ``PER_PIECE`` and ``PER_KG`` both multiply the rate by a quantity in the
    line's own unit of measure - the difference is which unit the rate card was
    agreed in, and it is recorded so an invoice can be checked against the
    order. ``LOT`` is a flat charge for the consignment however much comes back,
    which is why it is accrued once rather than per unit.
    """

    PER_PIECE = "PER_PIECE"
    PER_KG = "PER_KG"
    LOT = "LOT"


class JobWorkOrderStatus(StrEnum):
    """Lifecycle of a job work order (database.md section 7).

    ``CANCELLED`` is reachable only before the material has left the premises.
    Once stock is in the job-work pool at a vendor, the order is the only record
    of where it went: cancelling would orphan a quantity that is still on the
    company's books with nothing to explain it. An order whose material never
    comes back is closed after a final receipt writes the shortfall off as
    scrap, so the loss is stated rather than implied.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ISSUED = "ISSUED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    QC_PENDING = "QC_PENDING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class JobWorkSourceType(StrEnum):
    """What demand this order was raised to cover.

    Read the way procurement reads its own source pair: two orders naming the
    same source are covering the same shortage twice.
    """

    MANUAL = "MANUAL"
    PRODUCTION_ORDER = "PRODUCTION_ORDER"
    SALES_ORDER = "SALES_ORDER"
    ASSEMBLY_ORDER = "ASSEMBLY_ORDER"
    MRP_SUGGESTION = "MRP_SUGGESTION"
    REWORK = "REWORK"


class RejectionAction(StrEnum):
    """What happens to output the vendor produced badly.

    Three dispositions, each with exactly one posting rule, because "rejected"
    on its own does not say where the goods physically are:

    ``RETURN_TO_STOCK``   the bad pieces came back. They are received into the
                          ``DAMAGED`` pool - present, counted, not promisable -
                          and their share of the input is drawn out of the
                          job-work pool.
    ``REWORK_AT_VENDOR``  the vendor keeps them and reprocesses them. Nothing
                          moves: the input stays in the job-work pool, the line
                          stays open, and the quantity keeps appearing in
                          ``pending-at-vendor`` with its true ageing. This is
                          what makes rework traceable instead of a note in a
                          comment box.
    ``SCRAP_AT_VENDOR``   they were destroyed there. The input share moves from
                          the job-work pool to ``SCRAP`` and never comes home.
    """

    RETURN_TO_STOCK = "RETURN_TO_STOCK"
    REWORK_AT_VENDOR = "REWORK_AT_VENDOR"
    SCRAP_AT_VENDOR = "SCRAP_AT_VENDOR"


#: Statuses in which company-owned material is physically at the vendor. The
#: pending-at-vendor feed, the ageing report and the partial index on
#: ``job_work_orders`` are all built for exactly this set, so the tuple and the
#: index have to change together.
MATERIAL_AT_VENDOR_STATUSES: tuple[JobWorkOrderStatus, ...] = (
    JobWorkOrderStatus.ISSUED,
    JobWorkOrderStatus.PARTIALLY_RECEIVED,
)

#: Statuses in which the order still owes processed stock - what MRP nets
#: against as incoming supply and what the job-work board shows as open.
OPEN_SUPPLY_STATUSES: tuple[JobWorkOrderStatus, ...] = (
    JobWorkOrderStatus.APPROVED,
    JobWorkOrderStatus.ISSUED,
    JobWorkOrderStatus.PARTIALLY_RECEIVED,
    JobWorkOrderStatus.RECEIVED,
    JobWorkOrderStatus.QC_PENDING,
)

#: Statuses from which the order may still be cancelled outright - strictly
#: before any material has moved. See :class:`JobWorkOrderStatus`.
CANCELLABLE_STATUSES: tuple[JobWorkOrderStatus, ...] = (
    JobWorkOrderStatus.DRAFT,
    JobWorkOrderStatus.APPROVED,
)


def _sql_in_list(values: tuple[StrEnum, ...] | type[StrEnum]) -> str:
    """Render a status vocabulary as a SQL ``IN`` list.

    Generated from the Python enum so a database CHECK cannot drift away from
    the values the schemas and the state machine validate against.
    """
    members = values if isinstance(values, tuple) else tuple(values)
    return ", ".join(f"'{member.value}'" for member in members)


class JobWorkOrder(TenantEntity, OptimisticLockMixin):
    """One instruction to send material to a vendor and get it back processed."""

    __tablename__ = "job_work_orders"

    code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The processor. RESTRICT rather than CASCADE: the party who was holding
    #: company stock must survive as long as the record that they held it.
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    process_type: Mapped[JobWorkProcess] = mapped_column(String(24), nullable=False)
    #: What the vendor is actually being asked to do, in the operator's words -
    #: "PVD, rose gold, 3 micron". The process type classifies; this instructs,
    #: and it is what gets printed on the challan.
    process_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: The date the goods are due back. Drives the ageing buckets on the
    #: pending-at-vendor feed and the overdue alert.
    expected_return_date: Mapped[date] = mapped_column(Date, nullable=False)

    # --- where ---------------------------------------------------------------
    #: Material is drawn from here, and the job-work pool that holds it while it
    #: is away belongs to this position. Every ``JOB_WORK_ISSUE`` and every
    #: drawdown of the pool posts against this warehouse.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Where processed output is received. Usually the same store, but a coated
    #: finished good that goes straight to the finished-goods warehouse is
    #: ordinary, and defaulting the return to the source would put saleable
    #: stock in a raw-material store.
    return_warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- commercial ----------------------------------------------------------
    rate_per_unit: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )
    rate_basis: Mapped[RateBasis] = mapped_column(
        String(16), nullable=False, default=RateBasis.PER_PIECE, server_default=RateBasis.PER_PIECE
    )
    #: What the order is expected to cost, frozen from the rate and the planned
    #: quantity. Stored rather than recomputed so a later rate-card change does
    #: not silently restate what was approved.
    estimated_charges: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: What the receipts actually accrued. This is the figure finance consumes
    #: through ``job_work_charges_for_period`` - see the module docstring.
    total_charges: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Value of company material the vendor lost beyond the agreed allowance,
    #: at the material's own moving-average cost. Accrued by receipts and netted
    #: off the charge when the vendor is paid.
    vendor_debit_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- lifecycle -----------------------------------------------------------
    status: Mapped[JobWorkOrderStatus] = mapped_column(
        String(24),
        nullable=False,
        default=JobWorkOrderStatus.DRAFT,
        server_default=JobWorkOrderStatus.DRAFT.value,
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the material actually left. The ageing clock on everything still at
    #: the vendor runs from here, not from the order date.
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Sequence counter for this order's receipts, claimed under the header row
    #: lock. A child number taken from ``MAX(n) + 1`` races; a counter on a row
    #: that is already locked for the duration of the receipt cannot.
    receipt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # --- quality -------------------------------------------------------------
    #: Whether returned output must be inspected before it becomes usable.
    #: A per-document flag: ``items`` carries no inspection policy and inventing
    #: one would put the rule in two places that could disagree. The same choice
    #: ``goods_receipt_lines.requires_qc`` and ``assembly_orders.requires_qc``
    #: make.
    requires_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- provenance ----------------------------------------------------------
    source_type: Mapped[JobWorkSourceType] = mapped_column(
        String(24),
        nullable=False,
        default=JobWorkSourceType.MANUAL,
        server_default=JobWorkSourceType.MANUAL.value,
    )
    #: The demand document this order covers. Bare UUID: the demand lives in
    #: several tables and a polymorphic foreign key is not expressible in SQL.
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_job_work_orders_company_id_code"),
        # The job-work board: what is open, in due-date order.
        Index(
            "ix_job_work_orders_company_status_return",
            "company_id",
            "status",
            "expected_return_date",
        ),
        # "What is this vendor holding, and what have they done for us?" - the
        # vendor ledger and the vendor performance report both enter here.
        Index("ix_job_work_orders_vendor_status", "company_id", "vendor_id", "status"),
        Index("ix_job_work_orders_source", "company_id", "source_type", "source_id"),
        # THE index behind pending-at-vendor. Partial, because a mature
        # company's table is mostly closed orders that nobody chases.
        Index(
            "ix_job_work_orders_material_at_vendor",
            "company_id",
            "vendor_id",
            "expected_return_date",
            postgresql_where=text(f"status IN ({_sql_in_list(MATERIAL_AT_VENDOR_STATUSES)})"),
        ),
        CheckConstraint(f"status IN ({_sql_in_list(JobWorkOrderStatus)})", name="status_known"),
        CheckConstraint(
            f"process_type IN ({_sql_in_list(JobWorkProcess)})", name="process_type_known"
        ),
        CheckConstraint(f"rate_basis IN ({_sql_in_list(RateBasis)})", name="rate_basis_known"),
        CheckConstraint(
            f"source_type IN ({_sql_in_list(JobWorkSourceType)})", name="source_type_known"
        ),
        CheckConstraint("rate_per_unit >= 0", name="rate_non_negative"),
        CheckConstraint("estimated_charges >= 0", name="estimated_charges_non_negative"),
        CheckConstraint("total_charges >= 0", name="total_charges_non_negative"),
        CheckConstraint("vendor_debit_amount >= 0", name="vendor_debit_non_negative"),
        CheckConstraint("receipt_count >= 0", name="receipt_count_non_negative"),
        # Approval evidence is all-or-nothing: an approver with no timestamp
        # cannot be audited, and a timestamp with no approver names nobody.
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name="approval_evidence_complete",
        ),
        # "Why was this cancelled?" is the first question asked and the last
        # thing anyone remembers, so the row cannot exist without the answer.
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_orders_carry_a_reason",
        ),
    )


class JobWorkOrderLine(TenantEntity):
    """One material to send out, and the processed item expected back."""

    __tablename__ = "job_work_order_lines"

    job_work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("job_work_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Ordering and identity within the order. Printed on the challan, so it is
    #: stable rather than an array index that shifts when a line is removed.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The material sent out.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    quantity_to_issue: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: What physically left, posted as ``JOB_WORK_ISSUE``. The difference
    #: between this and ``received_quantity`` is what is still at the vendor,
    #: and it is matched, quantity for quantity, by
    #: ``inventory_balances.job_work_quantity`` at the order's warehouse.
    issued_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Input material accounted for out of the job-work pool: turned into
    #: output, returned unprocessed, or written off as scrap. This is the
    #: closing side of the traceability identity, not "goods received" - the
    #: processed goods are counted by ``received_output_quantity`` below,
    #: because they are usually a different item entirely.
    received_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: The processed item. A different code from ``item_id`` whenever the
    #: process changes what the thing is - a raw handle becomes a PVD-coated
    #: handle - and the same code when it does not (polishing, laser marking).
    expected_output_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Good output the order expects for the whole ``quantity_to_issue``. The
    #: ratio of the two is the yield, frozen here so a later master-data change
    #: cannot restate what an issued order was supposed to return.
    expected_output_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Good processed output actually received.
    received_output_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Output the vendor produced badly. Where it physically is depends on the
    #: receipt line's :class:`RejectionAction`.
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Input written off as process loss, posted as ``SCRAP`` out of the
    #: job-work pool. Included in ``received_quantity``.
    scrap_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Loss the process is agreed to consume - buffing removes metal, plating
    #: baths reject a few pieces. Loss within it is normal and posts as scrap
    #: with a reason; loss beyond it needs approval and debits the vendor.
    expected_loss_percentage: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: The ``stock_reservations`` row holding this material between approval and
    #: despatch. Bare UUID, no foreign key: ``allocation`` owns that table and a
    #: reservation released and retaken is a lifecycle this table does not
    #: arbitrate. Approving without reserving would let a sales order be promised
    #: the very stock a vendor is expecting on Monday.
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "job_work_order_id", "line_number", name="uq_job_work_order_lines_order_line_number"
        ),
        Index(
            "ix_job_work_order_lines_order", "company_id", "job_work_order_id", "line_number"
        ),
        # "Which orders have this part out at a vendor?" - shortage analysis and
        # the item's own stock story.
        Index("ix_job_work_order_lines_item", "company_id", "item_id"),
        # The other direction: "what is due back as this finished code?" - MRP's
        # supply netting reads the output item, not the input.
        Index("ix_job_work_order_lines_output_item", "company_id", "expected_output_item_id"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity_to_issue > 0", name="quantity_to_issue_positive"),
        CheckConstraint("expected_output_quantity > 0", name="expected_output_positive"),
        CheckConstraint("issued_quantity >= 0", name="issued_non_negative"),
        CheckConstraint("received_quantity >= 0", name="received_non_negative"),
        CheckConstraint("received_output_quantity >= 0", name="received_output_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
        CheckConstraint(
            "expected_loss_percentage >= 0 AND expected_loss_percentage <= 100",
            name="expected_loss_is_a_percentage",
        ),
        # Nothing may leave that was not ordered out. Over-issuing would put
        # stock at a vendor that no challan covers.
        CheckConstraint("issued_quantity <= quantity_to_issue", name="issued_within_ordered"),
        # THE traceability floor: material cannot be accounted back before it
        # was sent. If this constraint can be violated, the job-work pool and
        # this table disagree about how much of the company's stock is out.
        CheckConstraint("received_quantity <= issued_quantity", name="received_within_issued"),
        # Scrap is part of what was accounted for, never on top of it.
        CheckConstraint("scrap_quantity <= received_quantity", name="scrap_within_received"),
    )


class JobWorkChallan(TenantEntity):
    """The delivery challan the goods travel under.

    Statutory (GST rules 45 and 55): goods may not be sent to a job worker
    without one, it must state the parties, the goods and their taxable value,
    and the returns have to be reconciled against it. It is issued once, when
    the material leaves, and is never edited afterwards - a challan that can be
    amended after the vehicle has gone is not evidence of anything.
    """

    __tablename__ = "job_work_challans"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    #: RESTRICT, not CASCADE: a statutory document does not disappear because
    #: somebody deleted the order it was raised from.
    job_work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("job_work_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Denormalised from the order so the challan register can be filtered and
    #: totalled by vendor without joining, and so it still names the counterparty
    #: if the order is ever restructured.
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    challan_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_return_date: Mapped[date] = mapped_column(Date, nullable=False)

    vehicle_number: Mapped[str | None] = mapped_column(
        String(VEHICLE_NUMBER_LENGTH), nullable=True
    )
    #: Optional: goods handed to the vendor's own vehicle have no transporter.
    transporter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("transporters.id", ondelete="RESTRICT"), nullable=True
    )

    total_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Taxable value of the goods sent. Not a sale - no GST is charged on a job
    #: work challan - but the value has to be declared, and it is the figure the
    #: return is reconciled against. Costed from the stock the issue actually
    #: consumed, so it agrees with the ledger rather than with a price list.
    total_value: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_job_work_challans_company_id_code"),
        # The challan register, and the ITC-04 return that is filed from it.
        Index("ix_job_work_challans_company_date", "company_id", "challan_date"),
        Index("ix_job_work_challans_vendor_date", "company_id", "vendor_id", "challan_date"),
        Index("ix_job_work_challans_order", "company_id", "job_work_order_id"),
        CheckConstraint("total_quantity > 0", name="total_quantity_positive"),
        CheckConstraint("total_value >= 0", name="total_value_non_negative"),
        CheckConstraint(
            "expected_return_date >= challan_date", name="return_not_before_despatch"
        ),
    )


class JobWorkChallanLine(TenantEntity):
    """One material on the challan, with the quantity and value declared for it."""

    __tablename__ = "job_work_challan_lines"

    challan_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("job_work_challans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: RESTRICT: the order line is what the declared quantity is reconciled
    #: against, so it must outlive the challan that declared it.
    job_work_order_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("job_work_order_lines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: The position's moving-average cost at the moment of issue, captured from
    #: the ledger row the issue wrote. Frozen: the declared value of goods that
    #: have already left cannot be restated by a later re-average.
    unit_value: Mapped[Decimal] = mapped_column(
        UnitPrice, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_value: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    __table_args__ = (
        UniqueConstraint(
            "challan_id", "line_number", name="uq_job_work_challan_lines_challan_line_number"
        ),
        Index("ix_job_work_challan_lines_challan", "company_id", "challan_id", "line_number"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_value >= 0", name="unit_value_non_negative"),
        CheckConstraint("total_value >= 0", name="total_value_non_negative"),
    )


class JobWorkReceipt(TenantEntity):
    """One lot returning from the vendor.

    Its own row rather than a set of columns on the order: a vendor returns in
    lots, and each lot has its own arrival date, its own paperwork, its own
    charge and possibly its own inspection. Accumulating them into the header
    would lose the arrival dates that make a vendor measurable, and an
    over-receipt would be impossible to attribute to the delivery that caused it.
    """

    __tablename__ = "job_work_receipts"

    #: Derived from the order's code plus the header's ``receipt_count``
    #: (``JW/2026-27/00012/R2``), claimed under the order's row lock. There is
    #: no receipt series in ``DocTypes``, and inventing a company-wide one for a
    #: document that is only ever read in the context of its order would add a
    #: contended counter for nothing.
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: RESTRICT: the receipt is the evidence that stock came back, and it
    #: outlives any attempt to remove the order.
    job_work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("job_work_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Denormalised for the vendor ledger aggregate - see the module docstring.
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The vendor's own delivery document. Not unique and not validated: it is
    #: their number, recorded so the two sets of paperwork can be matched.
    vendor_challan_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_challan_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # --- what came back ------------------------------------------------------
    total_output_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_returned_unprocessed: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_scrap_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Input drawn out of the job-work pool by this receipt. The sum of this
    #: column over an order's receipts equals the sum of ``received_quantity``
    #: over its lines - the identity ``service.reconciliation`` reports on.
    total_input_consumed: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- money ---------------------------------------------------------------
    #: Processing charge this lot accrued: the agreed rate applied to the good
    #: output it delivered, or the lot charge if this is the receipt that
    #: earned it. Charges accrue here, not at issue - see the module docstring.
    total_charges: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Value of company material lost beyond the agreed allowance, priced at the
    #: material's own moving-average cost from the scrap movement this receipt
    #: posted. Netted off what the vendor is paid.
    vendor_debit_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- exceptions ----------------------------------------------------------
    #: True when any line lost more than its ``expected_loss_percentage``
    #: allowed. Such a receipt cannot be posted without the approval evidence
    #: below - see the CHECK constraint.
    has_excess_loss: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    excess_loss_approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    excess_loss_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- quality -------------------------------------------------------------
    #: When this lot's output was put on QC hold. Recorded even when ``quality``
    #: is not deployed, so the held stock is always explained.
    qc_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the verdict was recorded and the hold lifted. Held *and* not yet
    #: released is what "still awaiting inspection" means, so the release is its
    #: own column rather than clearing ``qc_held_at`` - blanking the hold date
    #: would destroy the evidence of how long the stock sat there.
    qc_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The ``qc_inspections`` row raised for this lot, when quality is deployed.
    #: Bare UUID, no foreign key - see the module docstring.
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_job_work_receipts_company_id_code"),
        UniqueConstraint(
            "job_work_order_id",
            "receipt_number",
            name="uq_job_work_receipts_order_receipt_number",
        ),
        Index("ix_job_work_receipts_order", "company_id", "job_work_order_id", "receipt_date"),
        # THE index behind the vendor ledger aggregate: charges for a vendor
        # over a period, without touching the orders table.
        Index("ix_job_work_receipts_vendor_date", "company_id", "vendor_id", "receipt_date"),
        # Lots held for inspection that nobody has been asked to inspect - the
        # queue quality picks up when it is deployed.
        Index(
            "ix_job_work_receipts_awaiting_inspection",
            "company_id",
            "qc_held_at",
            postgresql_where=text("qc_held_at IS NOT NULL AND qc_released_at IS NULL"),
        ),
        CheckConstraint("receipt_number > 0", name="receipt_number_positive"),
        CheckConstraint("total_output_quantity >= 0", name="output_non_negative"),
        CheckConstraint("total_rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("total_returned_unprocessed >= 0", name="returned_non_negative"),
        CheckConstraint("total_scrap_quantity >= 0", name="scrap_non_negative"),
        CheckConstraint("total_input_consumed > 0", name="input_consumed_positive"),
        CheckConstraint("total_charges >= 0", name="charges_non_negative"),
        CheckConstraint("vendor_debit_amount >= 0", name="vendor_debit_non_negative"),
        # Approval evidence is all-or-nothing, and an excess loss cannot be
        # posted without it. This is the constraint that makes "beyond the
        # allowance requires approval" a property of the data rather than a rule
        # somebody remembered to write in the service.
        CheckConstraint(
            "(excess_loss_approved_by_id IS NULL) = (excess_loss_approved_at IS NULL)",
            name="excess_loss_approval_evidence_complete",
        ),
        CheckConstraint(
            "NOT has_excess_loss OR excess_loss_approved_by_id IS NOT NULL",
            name="excess_loss_requires_approval",
        ),
        CheckConstraint(
            "vendor_challan_date IS NULL OR vendor_challan_date <= receipt_date",
            name="vendor_challan_not_after_receipt",
        ),
        # A hold cannot be lifted from a lot that was never held.
        CheckConstraint(
            "qc_released_at IS NULL OR qc_held_at IS NOT NULL",
            name="release_requires_a_hold",
        ),
    )


class JobWorkReceiptLine(TenantEntity):
    """What one order line gave back in one lot, and where each part of it went.

    The five quantities are not independent. Whatever this line consumed out of
    the job-work pool is exactly what it accounted for::

        input_consumed = input_equivalent(output + rejected_returned)
                       + returned_unprocessed
                       + scrap

    with ``input_equivalent`` dividing by the line's frozen yield ratio. Output
    the vendor keeps for rework consumes nothing: the material stays in the pool
    and the order line stays open, which is what keeps it traceable.
    """

    __tablename__ = "job_work_receipt_lines"

    receipt_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("job_work_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: RESTRICT: the order line is the outstanding-balance record this receipt
    #: drew down, so it must outlive the receipt.
    job_work_order_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("job_work_order_lines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Snapshot of the order line's output item. Held here so a receipt can be
    #: read on its own, and so the ledger row this line posted can be tied back
    #: to it without a join.
    output_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )

    #: Good processed output, received into ``AVAILABLE`` - or into ``QC_HOLD``
    #: when the order requires inspection.
    output_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Output the vendor produced badly. ``rejection_action`` says where it is.
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejection_action: Mapped[RejectionAction] = mapped_column(
        String(24),
        nullable=False,
        default=RejectionAction.RETURN_TO_STOCK,
        server_default=RejectionAction.RETURN_TO_STOCK.value,
    )
    #: Input material handed back untouched - the vendor could not process it,
    #: or the order was cut short. Goes straight back to ``AVAILABLE`` as the
    #: original item.
    returned_unprocessed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Input consumed by the process and gone: buffing swarf, plating rejects
    #: destroyed at the vendor. Posted as ``SCRAP`` out of the job-work pool.
    scrap_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: The part of ``scrap_quantity`` that exceeded the line's agreed allowance.
    #: This is what makes a receipt require approval and what the vendor debit
    #: is priced from.
    excess_loss_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Total drawn out of the job-work pool by this line - the identity in the
    #: class docstring, stored so the reconciliation does not have to recompute
    #: it from four columns and a ratio.
    input_consumed_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    #: Processing charge this line accrued, at the order's agreed rate.
    charges: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Value of the excess loss, at the material's moving-average cost taken
    #: from the scrap movement this line posted.
    vendor_debit_amount: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "receipt_id", "line_number", name="uq_job_work_receipt_lines_receipt_line_number"
        ),
        Index("ix_job_work_receipt_lines_receipt", "company_id", "receipt_id", "line_number"),
        # "Everything ever received against this order line" - the per-line
        # reconciliation and the vendor's delivery history.
        Index(
            "ix_job_work_receipt_lines_order_line", "company_id", "job_work_order_line_id"
        ),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint(
            f"rejection_action IN ({_sql_in_list(RejectionAction)})",
            name="rejection_action_known",
        ),
        CheckConstraint("output_quantity >= 0", name="output_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("returned_unprocessed_quantity >= 0", name="returned_non_negative"),
        CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
        CheckConstraint("excess_loss_quantity >= 0", name="excess_loss_non_negative"),
        CheckConstraint("charges >= 0", name="charges_non_negative"),
        CheckConstraint("vendor_debit_amount >= 0", name="vendor_debit_non_negative"),
        # A receipt line that consumed nothing and returned nothing is a row
        # nobody meant to write.
        CheckConstraint("input_consumed_quantity > 0", name="input_consumed_positive"),
        # The excess is a part of the loss, never additional to it.
        CheckConstraint(
            "excess_loss_quantity <= scrap_quantity", name="excess_loss_within_scrap"
        ),
        # Scrap and unprocessed returns are both drawn from the pool, so neither
        # can exceed what the line took out of it.
        CheckConstraint(
            "scrap_quantity + returned_unprocessed_quantity <= input_consumed_quantity",
            name="drawdown_components_within_consumed",
        ),
    )


__all__ = [
    "CANCELLABLE_STATUSES",
    "MATERIAL_AT_VENDOR_STATUSES",
    "MAX_LINES_PER_ORDER",
    "OPEN_SUPPLY_STATUSES",
    "VEHICLE_NUMBER_LENGTH",
    "JobWorkChallan",
    "JobWorkChallanLine",
    "JobWorkOrder",
    "JobWorkOrderLine",
    "JobWorkOrderStatus",
    "JobWorkProcess",
    "JobWorkReceipt",
    "JobWorkReceiptLine",
    "JobWorkSourceType",
    "RateBasis",
    "RejectionAction",
]
