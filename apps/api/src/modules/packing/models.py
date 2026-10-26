"""Packing orders, cartons and what is inside each carton.

Master rules section 22. The point of this module is not paperwork: it is the
sentence "which carton contains which serial", answerable months later from a
customer complaint. Everything below is shaped by that, and by the fact that a
carton is a real, purchased thing.

**A carton consumes packing material.** ``cartons.packing_material_item_id``
names the item a box is made from and ``packing_material_quantity`` says how many
of it this carton used. Sealing posts a ``MATERIAL_ISSUE`` for that item through
``inventory.post_movements`` and stores the resulting ledger row id in
``material_transaction_id``. A carton with no ledger row behind it would make the
packing-material balance drift permanently away from the shelf, so the CHECK
constraint refuses a sealed carton that has not posted one.

**Sealing is the freeze point, and it is one-way.** Before it, a carton can be
re-weighed, refilled or removed and nothing has been consumed. After it, the
contents are the physical truth inside a closed box: ``is_sealed`` and
``sealed_at`` move together (constrained), and no content row may be added,
changed or deleted. Removing a sealed carton is refused rather than reversed,
because unsealing would need an offsetting material movement for a box that has
already been taped shut.

**Weights are stored, not derived on read.** ``carton_contents.net_weight_kg``
is what the packer weighed for that quantity of that item; the carton's
``net_weight_kg`` is their sum and its ``gross_weight_kg`` is that plus
``tare_weight_kg`` - the weight of the empty box itself. The item master carries
no weight column, so there is nothing to derive this from; recomputing it on
every read would also mean a re-weighed carton silently restating a gross weight
a transporter has already quoted against. ``gross >= net`` is a CHECK on both
the carton and the order.

**``serial_ids`` is an array on the content row, not a child table.** Same
reasoning as ``assembly_order_components.consumed_serial_ids``: the set is small
(one serial per unit), it is always read with its content row, and it is written
once when the content is packed. The two CHECK constraints are what make it
trustworthy - a non-serialised item cannot carry serials at all, and a serialised
one must carry exactly as many as it packed - and the GIN index is what makes
"which carton holds serial X" a lookup rather than a full scan.

**``dispatch_id`` is a bare UUID with no foreign key.** It is the handover
marker, exactly as ``assembly_orders.packing_order_id`` is for the packing
handover: a PACKED order that no dispatch has claimed is work waiting to be
shipped. ``dispatch`` is deployed separately from this module and a hard
reference would make this table uncreatable until it lands. The partial index
below is built for precisely that predicate; see ``service.ready_for_dispatch``.

**Packing moves no finished-goods stock.** Completing an order does not dispatch
it (section 22) - the only ledger this module writes is the packing material it
eats. The goods leave the building when ``dispatch`` posts ``DISPATCH``
movements, and posting them here as well would decrement the same stock twice.
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
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import OptimisticLockMixin, TenantEntity, sql_in_list
from src.core.types import GUID, Quantity

#: A packing order with more lines than this is a bulk import or a data error,
#: not a delivery anybody packs. The bound matters because completion reads every
#: line and every carton of the order inside one transaction.
MAX_LINES_PER_ORDER = 200

#: Cartons in one order. A container load of small boxes is real; ten thousand is
#: a runaway loop. Completion sums all of them, so the set has to be bounded.
MAX_CARTONS_PER_ORDER = 1000

#: Distinct content rows in one carton. A carton holding more than this is being
#: used as a warehouse, and the label would be unprintable anyway.
MAX_CONTENTS_PER_CARTON = 200

#: ``cartons.barcode`` width. 64 leaves room for a document-derived body plus the
#: check character while staying inside a Code 128 label at a readable density.
BARCODE_LENGTH = 64

CARTON_TYPE_LENGTH = 24


class PackingOrderStatus(StrEnum):
    """Lifecycle of a packing order (master rules section 22).

    ``PACKED`` means the goods are boxed, sealed and weighed - not that they have
    left. ``HANDED_OVER`` is the moment a dispatch takes responsibility for them,
    and it is the dispatch module that sets it. ``CANCELLED`` is reachable only
    before ``PACKED``: past that, cartons are sealed and packing material has
    been consumed, so the way out is to close the order rather than to claim it
    never happened.
    """

    DRAFT = "DRAFT"
    IN_PROGRESS = "IN_PROGRESS"
    PACKED = "PACKED"
    HANDED_OVER = "HANDED_OVER"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class CartonType(StrEnum):
    """Physical form of the outer pack.

    ASSUMPTION: the contract does not fix this vocabulary. These are the forms a
    hardware manufacturer actually ships in. The value is descriptive - it drives
    the label and the freight paperwork, and no rule in this module branches on
    it - so extending the list is a one-line change here plus the CHECK below.
    """

    BOX = "BOX"
    CARTON = "CARTON"
    CRATE = "CRATE"
    PALLET = "PALLET"
    BAG = "BAG"
    DRUM = "DRUM"
    SHRINK_WRAP = "SHRINK_WRAP"


#: Statuses in which cartons may be added, filled, sealed or removed. Exactly
#: one - a draft has no cartons yet and a packed order's are sealed.
CARTON_EDITABLE_STATUSES: tuple[PackingOrderStatus, ...] = (PackingOrderStatus.IN_PROGRESS,)

#: Statuses from which the order may still be cancelled outright. See the status
#: docstring for why ``PACKED`` is not among them.
CANCELLABLE_STATUSES: tuple[PackingOrderStatus, ...] = (
    PackingOrderStatus.DRAFT,
    PackingOrderStatus.IN_PROGRESS,
)

#: Statuses in which the order still owes packed goods to somebody. The packing
#: board and the sales-order fulfilment view both read exactly this set.
OPEN_STATUSES: tuple[PackingOrderStatus, ...] = (
    PackingOrderStatus.DRAFT,
    PackingOrderStatus.IN_PROGRESS,
    PackingOrderStatus.PACKED,
)


class PackingOrder(TenantEntity, OptimisticLockMixin):
    """One instruction to box a set of goods for one customer."""

    __tablename__ = "packing_orders"

    # --- identity -----------------------------------------------------------
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Nullable: stock is packed against a sales order most of the time, but a
    #: sample shipment, a warranty replacement or a stock movement to a dealer is
    #: packed too and has no order behind it. RESTRICT because a packing list
    #: printed against an order must stay reconcilable to it.
    sales_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: NOT NULL even when there is no sales order: a carton is addressed to
    #: somebody, and the packing list names them.
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Where the goods are picked from and where the packing material is
    #: consumed. The material movement has to name a warehouse, so it is
    #: mandatory.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- lifecycle ----------------------------------------------------------
    status: Mapped[PackingOrderStatus] = mapped_column(
        String(24),
        nullable=False,
        default=PackingOrderStatus.DRAFT,
        server_default=PackingOrderStatus.DRAFT.value,
    )

    #: When the packing bench is scheduled to do this. A date, not a timestamp:
    #: it is a day's work list, and storing a spurious time would make two
    #: identical plans sort differently.
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: The instant packing was completed. A timestamp rather than a date because
    #: it is the clock a transporter's cut-off is measured against.
    packed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Who signed the order off as packed. Distinct from ``updated_by_id``, which
    #: any later edit overwrites; this one is written once and answers "who is
    #: accountable for what is in these boxes".
    packed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # --- totals, frozen at completion ---------------------------------------
    # Stored rather than aggregated on read: they are printed on the packing list
    # and quoted to a transporter, and a later carton correction must not
    # retroactively restate a number somebody has already been billed against.
    total_cartons: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_gross_weight: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_net_weight: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- dispatch handover ---------------------------------------------------
    #: The ``dispatches`` row that claimed this order. Bare UUID, no foreign key -
    #: see the module docstring.
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_packing_orders_company_id_code"),
        # The packing bench's day: "what am I packing today, oldest first".
        Index("ix_packing_orders_company_status_planned", "company_id", "status", "planned_date"),
        Index("ix_packing_orders_company_customer", "company_id", "customer_id"),
        Index("ix_packing_orders_company_warehouse_status", "company_id", "warehouse_id", "status"),
        # The dispatch handover feed, and the reason it stays cheap: a partial
        # index over only the rows the feed can ever return. See
        # ``service.ready_for_dispatch``, whose predicate this mirrors exactly -
        # the two have to change together.
        Index(
            "ix_packing_orders_ready_for_dispatch",
            "company_id",
            "packed_date",
            postgresql_where=text("status = 'PACKED' AND dispatch_id IS NULL"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(PackingOrderStatus)})", name="status_is_known"),
        CheckConstraint("total_cartons >= 0", name="total_cartons_non_negative"),
        CheckConstraint("total_gross_weight >= 0", name="total_gross_weight_non_negative"),
        CheckConstraint("total_net_weight >= 0", name="total_net_weight_non_negative"),
        # Gross is net plus the weight of the boxes themselves, so it can never
        # be the smaller number. A violation means the totals were assembled from
        # two different carton sets.
        CheckConstraint("total_gross_weight >= total_net_weight", name="total_gross_covers_net"),
        # Half a handover - a dispatch id with no timestamp, or the reverse - is
        # a row nothing can interpret, and the ready-for-dispatch feed would
        # either leak it or lose it.
        CheckConstraint(
            "(dispatch_id IS NULL) = (handed_over_at IS NULL)", name="handover_is_complete"
        ),
    )


class PackingOrderLine(TenantEntity):
    """One item the order has to get into boxes, and how far that has got."""

    __tablename__ = "packing_order_lines"

    packing_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("packing_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Stable identity within the order. Printed on the packing list and quoted
    #: when a carton content is attributed back to a line, so it is a real number
    #: rather than an array index that shifts when a line is removed.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The demand this line is filling, when there is one. Nullable for the same
    #: reason ``sales_order_id`` is on the header, and RESTRICT because the
    #: fulfilment arithmetic on the sales line has to stay reconcilable to it.
    sales_order_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("sales_order_lines.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity_to_pack: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Maintained by the carton contents, never typed in. Every content row that
    #: is added or removed adjusts it inside the same transaction.
    packed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    #: The FINAL inspection starting the order raised for this line, when its
    #: item requires final QC. A bare UUID: quality owns the row, and this
    #: module reaches it through the owner callback, never a join.
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    #: Exact physical units covered by the current FINAL inspection. Cartons
    #: cannot substitute another unit after a quantity-only approval.
    qc_serial_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, default=list, server_default=text("'{}'::uuid[]")
    )
    qc_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "packing_order_id", "line_number", name="uq_packing_order_lines_order_line_number"
        ),
        Index("ix_packing_order_lines_order", "company_id", "packing_order_id", "line_number"),
        Index("ix_packing_order_lines_item", "company_id", "item_id"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("quantity_to_pack > 0", name="quantity_to_pack_positive"),
        CheckConstraint("packed_quantity >= 0", name="packed_quantity_non_negative"),
        # Over-packing is refused rather than clamped: the difference is either a
        # mis-keyed quantity or goods nobody ordered, and both need a person.
        CheckConstraint("packed_quantity <= quantity_to_pack", name="packed_within_requested"),
        CheckConstraint(
            "cardinality(qc_serial_ids) = 0 OR cardinality(qc_serial_ids) = quantity_to_pack",
            name="qc_serials_cover_quantity",
        ),
        CheckConstraint(
            "qc_batch_id IS NULL OR cardinality(qc_serial_ids) > 0",
            name="qc_batch_has_serials",
        ),
    )


class Carton(TenantEntity):
    """One physical box: its barcode, its weight and whether it is closed."""

    __tablename__ = "cartons"

    packing_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("packing_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 1, 2, 3 ... within this order. It is what the label prints as "carton 3 of
    #: 7" and what a receiving clerk reads back over the phone, so it is
    #: sequential per order rather than globally unique.
    carton_number: Mapped[int] = mapped_column(Integer, nullable=False)

    carton_type: Mapped[CartonType] = mapped_column(
        String(CARTON_TYPE_LENGTH),
        nullable=False,
        default=CartonType.BOX,
        server_default=CartonType.BOX.value,
    )

    #: Server-generated, never accepted from a client, and unique per company.
    #: Derived from the order code and the carton number, so it is structurally
    #: unique before the index has to prove it - see ``service.build_barcode``.
    barcode: Mapped[str] = mapped_column(String(BARCODE_LENGTH), nullable=False)

    # --- dimensions, for volumetric freight ---------------------------------
    # Nullable as a set: a standard box's dimensions come from the packing
    # material and are often not re-measured. The CHECK below refuses a partial
    # measurement, because two of three sides computes no volume.
    length_cm: Mapped[Decimal | None] = mapped_column(Quantity, nullable=True)
    width_cm: Mapped[Decimal | None] = mapped_column(Quantity, nullable=True)
    height_cm: Mapped[Decimal | None] = mapped_column(Quantity, nullable=True)

    # --- weights -------------------------------------------------------------
    #: The empty box. Captured when the carton is opened, because afterwards it
    #: is under the goods and cannot be weighed on its own.
    tare_weight_kg: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Sum of the contents' weights. Maintained by the content rows.
    net_weight_kg: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Net plus tare, computed and frozen at sealing.
    gross_weight_kg: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- packing material this box cost -------------------------------------
    #: The item the box itself is. RESTRICT: the item a consumed quantity was
    #: posted against must survive as long as the ledger row does.
    packing_material_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    packing_material_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("1"), server_default="1"
    )
    #: The ``inventory_transactions`` row the sealing posted. Bare UUID for the
    #: same reason the ledger's own ``reference_id`` is one: the ledger outlives
    #: its source, and a foreign key here would invert that.
    material_transaction_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    # --- seal ----------------------------------------------------------------
    is_sealed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "packing_order_id", "carton_number", name="uq_cartons_order_carton_number"
        ),
        # A barcode is scanned to find one box. Two boxes answering to the same
        # scan is the failure this constraint exists to make impossible.
        UniqueConstraint("company_id", "barcode", name="uq_cartons_company_id_barcode"),
        Index("ix_cartons_order", "company_id", "packing_order_id", "carton_number"),
        # "Which boxes are still open on this bench?" - the packer's own list,
        # and what completion checks before it will finish an order.
        Index(
            "ix_cartons_open",
            "company_id",
            "packing_order_id",
            postgresql_where=text("is_sealed = false"),
        ),
        CheckConstraint("carton_number > 0", name="carton_number_positive"),
        CheckConstraint(f"carton_type IN ({sql_in_list(CartonType)})", name="carton_type_is_known"),
        # Structural shape of a generated barcode. Not the checksum - that is
        # verified in Python - but it stops anything empty or hand-written.
        CheckConstraint("barcode ~ '^[0-9A-Z-]{8,64}$'", name="barcode_is_scannable"),
        CheckConstraint("tare_weight_kg >= 0", name="tare_weight_non_negative"),
        CheckConstraint("net_weight_kg >= 0", name="net_weight_non_negative"),
        CheckConstraint("gross_weight_kg >= 0", name="gross_weight_non_negative"),
        CheckConstraint("gross_weight_kg >= net_weight_kg", name="gross_covers_net"),
        CheckConstraint("packing_material_quantity > 0", name="packing_material_quantity_positive"),
        # All three sides or none. Two of three measures nothing.
        CheckConstraint(
            "(length_cm IS NULL) = (width_cm IS NULL) AND (width_cm IS NULL) = (height_cm IS NULL)",
            name="dimensions_are_complete",
        ),
        CheckConstraint(
            "length_cm IS NULL OR (length_cm > 0 AND width_cm > 0 AND height_cm > 0)",
            name="dimensions_positive",
        ),
        CheckConstraint("is_sealed = (sealed_at IS NOT NULL)", name="seal_is_timestamped"),
        # A sealed carton has eaten a box off the shelf. Without the ledger row
        # the packing-material balance drifts and nobody can prove why.
        CheckConstraint(
            "NOT is_sealed OR material_transaction_id IS NOT NULL",
            name="sealed_carton_posted_its_material",
        ),
    )


class CartonContent(TenantEntity):
    """What is inside one carton - the row that makes traceability answerable."""

    __tablename__ = "carton_contents"

    carton_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("cartons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Which line of the order this quantity is filling. NOT NULL: a carton
    #: cannot contain goods the order does not ask for, and without the line the
    #: packed quantity could not be attributed when the same item appears on two
    #: lines of one order - which is ordinary when the sales order priced them
    #: differently. RESTRICT so a line cannot be dropped out from under a
    #: physical box.
    packing_order_line_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("packing_order_lines.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised from the line and validated against it on write. It is here
    #: because "which cartons hold this item" must not need a join through the
    #: order to answer.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Weight of exactly this quantity of this item. The item master has no
    #: weight column, so there is nothing to derive it from: the packer weighs it.
    net_weight_kg: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    #: Frozen at write time from the item master, so a later change to the item's
    #: tracking mode cannot invalidate the CHECK constraints on a box that has
    #: already shipped.
    is_serial_tracked_item: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The exact serialised units in this box. This is the column section 22 is
    #: about: given a serial, the carton, the order and the customer follow.
    serial_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, server_default=text("'{}'::uuid[]"), default=list
    )

    __table_args__ = (
        Index("ix_carton_contents_carton", "company_id", "carton_id"),
        Index("ix_carton_contents_item", "company_id", "item_id"),
        Index("ix_carton_contents_line", "company_id", "packing_order_line_id"),
        # Traceability in the other direction: given a serial, which carton?
        # A btree cannot answer a containment query on an array.
        Index("ix_carton_contents_serials", "serial_ids", postgresql_using="gin"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("net_weight_kg >= 0", name="net_weight_non_negative"),
        # An item with no serial numbers cannot have any in the box.
        CheckConstraint(
            "is_serial_tracked_item OR cardinality(serial_ids) = 0",
            name="serials_only_on_serialised_items",
        ),
        # And a serialised item must name exactly the units packed: one serial
        # per unit. Without this the traceability array could fall behind the
        # quantity and nobody would notice until a recall.
        CheckConstraint(
            "NOT is_serial_tracked_item OR cardinality(serial_ids) = quantity",
            name="serial_count_matches_quantity",
        ),
    )


__all__ = [
    "BARCODE_LENGTH",
    "CANCELLABLE_STATUSES",
    "CARTON_EDITABLE_STATUSES",
    "CARTON_TYPE_LENGTH",
    "MAX_CARTONS_PER_ORDER",
    "MAX_CONTENTS_PER_CARTON",
    "MAX_LINES_PER_ORDER",
    "OPEN_STATUSES",
    "Carton",
    "CartonContent",
    "CartonType",
    "PackingOrder",
    "PackingOrderLine",
    "PackingOrderStatus",
]
