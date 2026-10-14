"""Assembly orders: components in, one finished assembly out.

Master rules section 19. The canonical example is a Lock Set: a Handle, a
Cylinder, a Lock body, a set of Keys and a Screw kit go in, one saleable Lock
Set comes out. Six modelling decisions in this file carry the weight.

**Consumption is posted once, at completion.** ``issued_quantity`` records the
physical hand-over of components to the line; it moves no stock. The ledger only
learns about the assembly when the assembly actually happened, at which point
the ``ASSEMBLY_CONSUME`` rows for the components and the ``ASSEMBLY_RECEIPT``
row for the output are written together (database.md section 8). Posting the
issue as a movement *and* the consumption as a movement would decrement the same
components twice; leaving the components unreserved in between would let another
document promise parts that are already sitting on the bench. So the window
between issue and completion is covered by a ``stock_reservations`` row instead,
which is what ``reservation_id`` on the component holds.

**``consumed_serial_ids`` is an array on the component, not a child table.** A
lock set assembled from specific serialised cylinders has to stay traceable to
exactly those cylinders for the life of the product. The set is small (one
serial per unit consumed), it is always read with its component row, and it is
written once at completion - a child table would add a join to every read of the
document and buy nothing. The CHECK constraints below are what make it
trustworthy: a non-serialised component cannot carry serials at all, and a
serialised one must carry exactly as many as it consumed.

**``bom_id`` is mandatory.** An assembly order says "build this, from this
structure". Allowing a free-form component list would mean stock is consumed
against a recipe nobody approved, and the consumption could never be reconciled
against engineering's view of what the product contains.

**Two warehouses, both mandatory.** Components are drawn from ``warehouse_id``
and the finished assembly is received into ``output_warehouse_id``. They are
usually the same, but a line that feeds a separate finished-goods store is
ordinary, and defaulting the output to the component source would silently put
saleable stock in a raw-material warehouse.

**``qc_inspection_id`` is a bare UUID with no foreign key**, and ``qc_held_at``
records the hold independently of it. ``quality`` is deployed separately from
this module; a hard reference would make this table uncreatable until it lands.
More importantly the two facts are genuinely different: "this output is on QC
hold" is decided here and is always true when ``requires_qc``, while "an
inspection document exists for it" is only true once quality is deployed to
create one. Same reasoning as the inventory ledger's ``reference_id``
(database.md section 4.2).

**``packing_order_id`` is a bare UUID owned by the packing module.** It is the
handover marker: a completed assembly with free output stock and no packing
order against it is work waiting to be packed. See ``service.ready_for_packing``
and the partial index built for exactly that predicate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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

#: An assembly order with more component lines than this is a data error or a
#: bulk import, not a bill of materials anybody assembles at a bench. The bound
#: matters because completion posts every component in one locked transaction.
MAX_COMPONENTS_PER_ORDER = 200

#: Free-text length for the physical line or bench the order is scheduled on.
ASSEMBLY_LINE_LENGTH = 64

#: Default scheduling and reservation priority. Lower is more urgent, matching
#: ``allocation.models.DEFAULT_RESERVATION_PRIORITY`` and the production order's
#: default, so a reallocation can weigh an assembly against a sales order or a
#: production order on one scale.
DEFAULT_ASSEMBLY_PRIORITY = 100


class AssemblyOrderStatus(StrEnum):
    """Lifecycle of an assembly order (master rules section 19).

    ``CANCELLED`` is reachable only before the components have been issued.
    Once parts are on the bench and reserved against the order, the way out is
    to complete it (possibly with everything rejected) or to close it after
    returning the parts - cancelling would leave reservations belonging to a
    document that claims never to have existed.

    ``QC_PENDING`` sits between assembly and completion for outputs that need
    inspecting. The stock exists by then, but on QC hold rather than available.
    """

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RELEASED = "RELEASED"
    COMPONENTS_ISSUED = "COMPONENTS_ISSUED"
    IN_PROGRESS = "IN_PROGRESS"
    QC_PENDING = "QC_PENDING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class AssemblySourceType(StrEnum):
    """What demand this assembly order was raised to cover.

    Read the same way procurement reads its own source pair: two orders naming
    the same source demand are covering the same shortage twice.
    """

    MANUAL = "MANUAL"
    SALES_ORDER = "SALES_ORDER"
    MRP_SUGGESTION = "MRP_SUGGESTION"
    MRP_RUN = "MRP_RUN"
    PRODUCTION_ORDER = "PRODUCTION_ORDER"
    STOCK_REPLENISHMENT = "STOCK_REPLENISHMENT"


#: Statuses in which the order still owes finished stock. The scheduling board,
#: MRP's supply netting and the shop-floor list all read exactly this set, and
#: the partial index on ``assembly_orders`` is built for it - so the tuple and
#: the index have to change together.
OPEN_SUPPLY_STATUSES: tuple[AssemblyOrderStatus, ...] = (
    AssemblyOrderStatus.APPROVED,
    AssemblyOrderStatus.RELEASED,
    AssemblyOrderStatus.COMPONENTS_ISSUED,
    AssemblyOrderStatus.IN_PROGRESS,
    AssemblyOrderStatus.QC_PENDING,
)

#: Statuses in which components are physically on the line and reserved against
#: this order. Cancellation is refused here; see the class docstring.
COMPONENTS_HELD_STATUSES: tuple[AssemblyOrderStatus, ...] = (
    AssemblyOrderStatus.COMPONENTS_ISSUED,
    AssemblyOrderStatus.IN_PROGRESS,
)

#: Statuses from which the order may still be cancelled outright.
CANCELLABLE_STATUSES: tuple[AssemblyOrderStatus, ...] = (
    AssemblyOrderStatus.DRAFT,
    AssemblyOrderStatus.APPROVED,
    AssemblyOrderStatus.RELEASED,
)

#: Open statuses in which work on the bench has not begun. The assembly board
#: counts these as "not started", and one of them past its planned start is a
#: late start.
NOT_STARTED_STATUSES: tuple[AssemblyOrderStatus, ...] = (
    AssemblyOrderStatus.APPROVED,
    AssemblyOrderStatus.RELEASED,
    AssemblyOrderStatus.COMPONENTS_ISSUED,
)


class AssemblyOrder(TenantEntity, OptimisticLockMixin):
    """One instruction to assemble a quantity of one item from its components."""

    __tablename__ = "assembly_orders"

    # --- identity -----------------------------------------------------------
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The assembled output. Not the BOM's parent by accident - it is validated
    #: against it, because an order that builds one item from another item's
    #: recipe would consume the wrong parts.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The approved ``ASSEMBLY`` or ``PRODUCTION`` structure this order is built
    #: from. Mandatory - see the module docstring. RESTRICT rather than CASCADE:
    #: the recipe a consumed quantity was drawn against must survive as long as
    #: the order does.
    bom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("boms.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # --- quantities ---------------------------------------------------------
    planned_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Good output actually produced. Written once, at completion.
    assembled_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Output that was built and failed. It consumed components exactly as the
    #: good output did, which is why the two are added together to work out what
    #: the order actually ate.
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    # --- where ---------------------------------------------------------------
    #: Component source. Every ``ASSEMBLY_CONSUME`` posts against this warehouse.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Finished-goods destination. The ``ASSEMBLY_RECEIPT`` posts here.
    output_warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The physical line or bench. Free text rather than a foreign key: an
    #: assembly line is not a work centre in this plant's vocabulary, and
    #: modelling it as a master would force every site to maintain a table
    #: before it could raise its first order.
    assembly_line: Mapped[str | None] = mapped_column(String(ASSEMBLY_LINE_LENGTH), nullable=True)

    # --- schedule ------------------------------------------------------------
    planned_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Stamped when work actually begins and ends, so "are we late?" is answered
    #: against reality rather than against the plan.
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Lower is more urgent. Every component reservation taken at release carries
    #: it, together with the planned start as its required date, so allocation
    #: ranks this order's claim on a part the way the planner ranked the order.
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_ASSEMBLY_PRIORITY,
        server_default=str(DEFAULT_ASSEMBLY_PRIORITY),
    )

    # --- lifecycle -----------------------------------------------------------
    status: Mapped[AssemblyOrderStatus] = mapped_column(
        String(24),
        nullable=False,
        default=AssemblyOrderStatus.DRAFT,
        server_default=AssemblyOrderStatus.DRAFT.value,
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- quality -------------------------------------------------------------
    #: Whether the finished assembly must be inspected before it is saleable.
    #: A per-document flag, exactly as ``goods_receipt_lines.requires_qc`` is:
    #: the item master carries no inspection policy, and inventing one here
    #: would put a rule in two places that could disagree.
    requires_qc: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: When completion put the output on QC hold. Set independently of
    #: ``qc_inspection_id`` so the requirement is recorded even when the quality
    #: module is not deployed - see the module docstring.
    qc_held_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The ``qc_inspections`` row raised for this output, when quality is
    #: deployed. Bare UUID, no foreign key.
    qc_inspection_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    # --- packing handover ----------------------------------------------------
    #: The ``packing_orders`` row that took this assembly's output. Bare UUID,
    #: no foreign key: ``packing`` owns that table and is deployed separately.
    #: NULL is the "not yet packed" half of ``service.ready_for_packing``.
    packing_order_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- provenance ----------------------------------------------------------
    source_type: Mapped[AssemblySourceType] = mapped_column(
        String(24),
        nullable=False,
        default=AssemblySourceType.MANUAL,
        server_default=AssemblySourceType.MANUAL.value,
    )
    #: The demand document this order covers. Bare UUID: the demand lives in
    #: several tables and a polymorphic foreign key is not expressible in SQL.
    source_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_assembly_orders_company_id_code"),
        # The shop-floor board: what is open, in schedule order.
        Index(
            "ix_assembly_orders_company_status_planned",
            "company_id",
            "status",
            "planned_start",
        ),
        # "What is being assembled for this item?" - MRP's supply netting.
        Index("ix_assembly_orders_item_status", "company_id", "item_id", "status"),
        # Component availability is worked out a warehouse at a time.
        Index("ix_assembly_orders_warehouse_status", "company_id", "warehouse_id", "status"),
        Index("ix_assembly_orders_source", "company_id", "source_type", "source_id"),
        # THE index behind the open-supply feed. Partial, because a mature
        # company's table is mostly closed orders that planning never reads.
        Index(
            "ix_assembly_orders_open_supply",
            "company_id",
            "item_id",
            "planned_end",
            postgresql_where=text(f"status IN ({sql_in_list(OPEN_SUPPLY_STATUSES)})"),
        ),
        # THE index behind ready_for_packing: completed, unpacked output, by
        # destination warehouse. Partial for the same reason.
        Index(
            "ix_assembly_orders_ready_for_packing",
            "company_id",
            "output_warehouse_id",
            "item_id",
            postgresql_where=text("status = 'COMPLETED' AND packing_order_id IS NULL"),
        ),
        # Outputs held for inspection that nobody has been asked to inspect -
        # the queue quality picks up when it is deployed.
        Index(
            "ix_assembly_orders_awaiting_inspection",
            "company_id",
            "qc_held_at",
            postgresql_where=text("qc_held_at IS NOT NULL AND qc_inspection_id IS NULL"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(AssemblyOrderStatus)})", name="status_known"),
        CheckConstraint(
            f"source_type IN ({sql_in_list(AssemblySourceType)})", name="source_type_known"
        ),
        CheckConstraint("planned_quantity > 0", name="planned_quantity_positive"),
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        CheckConstraint("assembled_quantity >= 0", name="assembled_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        # The correctness floor on yield. Components are drawn for the planned
        # quantity; reporting more output than was planned would consume parts
        # that were never issued. Over-delivery is a new order, not a bigger one.
        CheckConstraint(
            "assembled_quantity + rejected_quantity <= planned_quantity",
            name="output_within_planned_quantity",
        ),
        CheckConstraint(
            "planned_end IS NULL OR planned_start IS NULL OR planned_end >= planned_start",
            name="planned_end_not_before_start",
        ),
        CheckConstraint(
            "actual_end IS NULL OR actual_start IS NULL OR actual_end >= actual_start",
            name="actual_end_not_before_start",
        ),
        # Approval evidence is all-or-nothing: an approver with no timestamp
        # cannot be audited, and a timestamp with no approver names nobody.
        CheckConstraint(
            "(approved_by_id IS NULL) = (approved_at IS NULL)",
            name="approval_evidence_complete",
        ),
        # An inspection or a hold against an order that never needed QC would
        # make the flag unreadable as the reason the stock is not available.
        CheckConstraint(
            "requires_qc OR (qc_held_at IS NULL AND qc_inspection_id IS NULL)",
            name="qc_evidence_requires_qc_flag",
        ),
        CheckConstraint(
            "(packing_order_id IS NULL) = (packed_at IS NULL)",
            name="packing_handover_complete",
        ),
        # "Why was this cancelled?" is the first question asked and the last
        # thing anyone remembers, so the row cannot exist without the answer.
        CheckConstraint(
            "status <> 'CANCELLED' OR cancellation_reason IS NOT NULL",
            name="cancelled_orders_carry_a_reason",
        ),
    )


class AssemblyOrderComponent(TenantEntity):
    """One part the assembly consumes, and what it actually ate."""

    __tablename__ = "assembly_order_components"

    assembly_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("assembly_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Ordering and identity within the order. Printed on the kitting list, so
    #: it is stable rather than an array index that shifts when a line changes.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    #: Scrap-adjusted requirement for the whole planned quantity, frozen from
    #: the BOM when the order was raised. Stored rather than recomputed: the
    #: structure may be revised next month and a released order must keep
    #: consuming against the recipe it was kitted from.
    required_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Physically handed to the line. Moves no stock on its own - see the module
    #: docstring - but bounds what completion is allowed to consume.
    issued_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: What the finished assemblies actually ate. This is the quantity the
    #: ``ASSEMBLY_CONSUME`` ledger rows carry.
    consumed_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: Snapshot of ``items.is_serial_tracked`` when the order was raised. The
    #: flag can be switched on the item master afterwards; the kitting rules for
    #: an order already on the bench must not change underneath it.
    is_serial_tracked_component: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The exact serialised units this assembly swallowed. A lock set built from
    #: specific cylinders stays traceable to them for the life of the product.
    consumed_serial_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(GUID), nullable=False, server_default=text("'{}'::uuid[]"), default=list
    )

    #: The ``stock_reservations`` row holding this component between issue and
    #: completion. Bare UUID, no foreign key: ``allocation`` owns that table and
    #: a reservation released and retaken is a lifecycle this table does not
    #: arbitrate.
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "assembly_order_id",
            "line_number",
            name="uq_assembly_order_components_order_line_number",
        ),
        Index(
            "ix_assembly_order_components_order",
            "company_id",
            "assembly_order_id",
            "line_number",
        ),
        # "Which assemblies are eating this part?" - shortage analysis.
        Index("ix_assembly_order_components_item", "company_id", "item_id"),
        # Traceability in the other direction: given a serial, which assembly
        # consumed it? A btree cannot answer a containment query on an array.
        Index(
            "ix_assembly_order_components_serials",
            "consumed_serial_ids",
            postgresql_using="gin",
        ),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint("required_quantity > 0", name="required_quantity_positive"),
        CheckConstraint("issued_quantity >= 0", name="issued_non_negative"),
        CheckConstraint("consumed_quantity >= 0", name="consumed_non_negative"),
        # Nothing may be consumed that was never handed to the line. This is the
        # constraint that makes ``issued_quantity`` mean something rather than
        # being a number somebody typed.
        CheckConstraint("consumed_quantity <= issued_quantity", name="consumed_within_issued"),
        # A part with no serial numbers cannot have consumed any.
        CheckConstraint(
            "is_serial_tracked_component OR cardinality(consumed_serial_ids) = 0",
            name="serials_only_when_serialised",
        ),
        # And a serialised part must name exactly the units it ate: one serial
        # per unit. Without this the traceability array could silently fall
        # behind the quantity and nobody would notice until a recall.
        CheckConstraint(
            "NOT is_serial_tracked_component "
            "OR cardinality(consumed_serial_ids) = consumed_quantity",
            name="serial_count_matches_consumed",
        ),
    )


__all__ = [
    "ASSEMBLY_LINE_LENGTH",
    "CANCELLABLE_STATUSES",
    "COMPONENTS_HELD_STATUSES",
    "DEFAULT_ASSEMBLY_PRIORITY",
    "MAX_COMPONENTS_PER_ORDER",
    "NOT_STARTED_STATUSES",
    "OPEN_SUPPLY_STATUSES",
    "AssemblyOrder",
    "AssemblyOrderComponent",
    "AssemblyOrderStatus",
    "AssemblySourceType",
]
