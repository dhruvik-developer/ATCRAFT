"""Bill of materials: the product structure the whole plant is planned from.

Four modelling decisions in this file are load-bearing, and each one exists
because the alternative produces a defect that is expensive to find later.

**Versions are rows, not edits.** A BOM is never rewritten in place once it has
been approved. ``(company_id, item_id, version)`` is unique and a new revision is
a new row created by ``copy-as-new-version``, so a production order raised in
March can still be reconciled against the structure that was current in March.

**"The" BOM for an item is a partial unique index, not application logic.**
At most one row per item may be ``is_default`` *and* ``APPROVED`` at the same
time. MRP, costing and production all resolve "which structure do I use?"
through that pair, and two winners would make three modules silently disagree.
PostgreSQL arbitrates it, so a concurrent approve cannot produce two.

**A line cannot name its own parent item.** ``bom_lines`` carries a denormalised
``parent_item_id`` tied to ``boms(id, item_id)`` by a composite foreign key, and
the self-loop is then a plain ``CHECK``. A CHECK constraint cannot reach into
another table, so without the denormalised column the rule could only live in
application code - and a direct self-loop is the one cycle cheap enough to stop
at write time rather than discovering it during an explosion. The composite FK is
what guarantees the copy cannot drift from ``boms.item_id``; consequently
``boms.item_id`` is immutable after creation.

**Routing is referenced by bare UUID.** ``bom_operations.routing_operation_id``
and ``work_center_id`` are plain ``UUID`` columns with no foreign key: the
``routing`` module owns ``routing_operations`` and ``work_centers`` and is built
independently of this one, so a hard FK here would make the schema uncreatable
until that module lands. The service validates the reference when routing is
present; the column carries the link either way. This is the same reasoning the
inventory ledger uses for ``reference_id`` (database.md section 4.2).
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
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import TenantEntity
from src.core.types import GUID, Money, Percentage, Quantity

#: Guard rail for :func:`src.modules.bom.engine.explode`. Twenty levels is far
#: deeper than any real architectural-hardware assembly; anything deeper is a
#: data error, and without the bound a cycle that slipped past validation would
#: run until the request timed out.
DEFAULT_MAX_EXPLOSION_DEPTH = 20

#: Scrap is a percentage of the required quantity. 100% would mean every piece
#: made is scrapped, which is a typo rather than a process.
MAX_SCRAP_PERCENTAGE = Decimal("100")


class BomStatus(StrEnum):
    """Lifecycle of one BOM version (database.md section 5).

    Only ``APPROVED`` structures are visible to planning. ``OBSOLETE`` is
    terminal: a superseded structure is retained because documents raised
    against it still reference it.
    """

    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    OBSOLETE = "OBSOLETE"


class BomType(StrEnum):
    """What the structure is used for.

    ``PHANTOM`` is the one with explosion semantics attached: a phantom is a
    drawing-level grouping that is never stocked, so the explosion blows through
    it and raises requirements for its children only (master rules section 11).
    ``ALTERNATE`` marks a structure that exists beside a default one - it carries
    ``parent_bom_id`` pointing at the BOM it is an alternative to.
    """

    PRODUCTION = "PRODUCTION"
    ASSEMBLY = "ASSEMBLY"
    PHANTOM = "PHANTOM"
    ALTERNATE = "ALTERNATE"


class Bom(TenantEntity):
    """One version of the structure that makes one item."""

    __tablename__ = "boms"

    # --- identity ------------------------------------------------------------
    code: Mapped[str] = mapped_column(String(64), nullable=False)

    # Immutable after creation: bom_lines carry a denormalised copy tied to this
    # column by a composite foreign key (see the module docstring), so changing
    # it would break every line at once.
    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    #: Engineering revision label ("A", "B", "R2"). Free text on purpose: it is
    #: the customer's or the drawing's revision, not ours, so it cannot be
    #: derived from ``version``.
    revision: Mapped[str | None] = mapped_column(String(16), nullable=True)

    status: Mapped[BomStatus] = mapped_column(
        String(24), nullable=False, default=BomStatus.DRAFT, server_default=BomStatus.DRAFT
    )
    bom_type: Mapped[BomType] = mapped_column(
        String(16), nullable=False, default=BomType.PRODUCTION, server_default=BomType.PRODUCTION
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    # --- what one run of this structure yields -------------------------------
    # Quantities on the lines are per `output_quantity` units, not per unit: a
    # structure that yields 10 handles from one billet cannot be expressed
    # correctly otherwise, and dividing at explosion time keeps the stored
    # numbers the ones the engineer typed.
    output_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("1"), server_default="1"
    )
    base_uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    # --- effectivity ---------------------------------------------------------
    # A structure change takes effect on a date, not on the moment somebody
    # clicked approve, so MRP asks "what was effective on the required date?".
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: Set on an ``ALTERNATE``: the default structure this one is an alternative
    #: to. RESTRICT rather than CASCADE - losing a default must not silently
    #: delete the alternates planned against it.
    parent_bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("boms.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # --- approval evidence (master rules section 42) -------------------------
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- rolled-up cost, refreshed on approval -------------------------------
    # Stored rather than always recomputed because production order costing and
    # the item list both read it, and a multi-level roll-up is not a per-request
    # calculation. GET /bom/{id}/cost recomputes live and does not write.
    total_material_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    total_operation_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    cost_rolled_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lost-update protection for a document several engineers edit at once
    # (database.md section 1). ``OptimisticLockMixin`` cannot be used: it names
    # its counter ``version``, and that name is already taken by the BOM's own
    # engineering version in the binding contract. Same mechanism, different
    # column - SQLAlchemy raises StaleDataError, which the handler turns into a
    # 409 rather than letting the second writer silently overwrite the first.
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}  # noqa: RUF012 - SQLAlchemy API

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_boms_company_id_code"),
        UniqueConstraint(
            "company_id", "item_id", "version", name="uq_boms_company_id_item_version"
        ),
        # Target for the bom_lines composite foreign key that makes the
        # self-loop CHECK possible. Not a business rule of its own - id is
        # already unique - but the FK cannot reference an unconstrained pair.
        UniqueConstraint("id", "item_id", name="uq_boms_id_item_id"),
        # THE rule the rest of the ERP depends on: one live default structure
        # per item. Enforced by PostgreSQL because two modules resolving
        # different BOMs for the same item is a silent, expensive divergence.
        Index(
            "uq_boms_one_default_approved_per_item",
            "company_id",
            "item_id",
            unique=True,
            postgresql_where=text("is_default AND status = 'APPROVED'"),
        ),
        # The explosion engine's hot path: "effective approved structures for
        # this set of items". Partial, because only APPROVED rows are ever
        # resolved this way and they are a minority of the table once a few
        # revisions exist.
        Index(
            "ix_boms_effective_lookup",
            "company_id",
            "item_id",
            "effective_from",
            "effective_to",
            postgresql_where=text("status = 'APPROVED'"),
        ),
        Index("ix_boms_company_status", "company_id", "status", "created_at"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("output_quantity > 0", name="output_quantity_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effectivity_window_ordered",
        ),
        CheckConstraint("total_material_cost >= 0", name="material_cost_non_negative"),
        CheckConstraint("total_operation_cost >= 0", name="operation_cost_non_negative"),
        CheckConstraint("parent_bom_id IS NULL OR parent_bom_id <> id", name="parent_bom_not_self"),
        # Approval evidence is all-or-nothing: a row claiming APPROVED with no
        # approver, or an approver with no timestamp, cannot be audited.
        CheckConstraint(
            "(status <> 'APPROVED') OR (approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="approved_rows_carry_approval_evidence",
        ),
    )


class BomLine(TenantEntity):
    """One component consumed by a BOM."""

    __tablename__ = "bom_lines"

    bom_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)
    #: Denormalised ``boms.item_id``, kept honest by the composite foreign key
    #: below. It exists so the self-loop rule can be a database CHECK.
    parent_item_id: Mapped[uuid.UUID] = mapped_column(GUID, nullable=False)

    #: Ordering and identity within the BOM. Printed on the shop-floor document,
    #: so it is stable and unique per BOM rather than an array index.
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    component_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    #: Quantity needed to produce ``boms.output_quantity`` of the parent - not
    #: per unit. Expressed in the component's stock unit; the service refuses
    #: any other unit so that multi-level consolidation keyed on item_id is
    #: arithmetically sound.
    quantity_per: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    uom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )

    #: Process loss on this component, as a percentage of the required quantity.
    #: The explosion inflates by it; the un-inflated figure is reported beside
    #: the inflated one so a planner can see what the loss allowance costs.
    scrap_percentage: Mapped[Decimal] = mapped_column(
        Percentage, nullable=False, default=Decimal("0"), server_default="0"
    )

    #: An optional component is configurable rather than structural, so demand
    #: for it comes from the order that selects it, not from the BOM.
    is_optional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Set when this line is an approved replacement for another line. Both
    #: lines are never required together, so substitutes are excluded from the
    #: explosion - counting them would double the requirement.
    substitute_for_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("bom_lines.id", ondelete="CASCADE"), nullable=True
    )

    #: Which operation consumes the component. Matches
    #: ``bom_operations.operation_sequence``; used to stage material issue by
    #: operation instead of dumping the whole kit at the start of the job.
    operation_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Composite FK: guarantees parent_item_id is genuinely boms.item_id for
        # this bom_id, which is what makes the CHECK below meaningful. CASCADE
        # so deleting a draft BOM takes its lines with it.
        ForeignKeyConstraint(
            ["bom_id", "parent_item_id"],
            ["boms.id", "boms.item_id"],
            name="fk_bom_lines_bom_id_parent_item_id_boms",
            ondelete="CASCADE",
        ),
        UniqueConstraint("bom_id", "line_number", name="uq_bom_lines_bom_id_line_number"),
        # The engine loads every line of a level's BOMs in one batched query.
        Index("ix_bom_lines_bom", "company_id", "bom_id", "line_number"),
        # Reverse explosion: "which structures use this screw?".
        Index("ix_bom_lines_component", "company_id", "component_item_id"),
        # No direct self-loop. Deeper cycles need the whole graph and are caught
        # during traversal (engine.explode raises BomCycleError); this catches
        # the one case that is decidable from a single row.
        CheckConstraint(
            "component_item_id <> parent_item_id", name="component_is_not_the_parent_item"
        ),
        CheckConstraint("quantity_per > 0", name="quantity_per_positive"),
        CheckConstraint("line_number > 0", name="line_number_positive"),
        CheckConstraint(
            f"scrap_percentage >= 0 AND scrap_percentage < {MAX_SCRAP_PERCENTAGE}",
            name="scrap_percentage_bounded",
        ),
        CheckConstraint(
            "substitute_for_line_id IS NULL OR substitute_for_line_id <> id",
            name="substitute_not_self",
        ),
        CheckConstraint(
            "operation_sequence IS NULL OR operation_sequence > 0",
            name="operation_sequence_positive",
        ),
    )


class BomOperation(TenantEntity):
    """One routing step costed against this BOM.

    Times and rates are stored on the BOM rather than read from ``routing``
    every time on purpose: a BOM is a costed snapshot of how the item was made
    at this revision, and a later rate change on the work centre must not
    retroactively alter the cost of a structure that was already approved.
    ``routing_operation_id`` keeps the link back to the live routing for anyone
    who wants to compare.
    """

    __tablename__ = "bom_operations"

    bom_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("boms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    operation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Plain UUIDs, deliberately not foreign keys - see the module docstring.
    routing_operation_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    work_center_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    #: Charged once per run of ``boms.output_quantity``, so the roll-up
    #: amortises it over that quantity rather than per piece.
    setup_time_minutes: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Charged per unit of output.
    run_time_minutes_per_unit: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Blended labour + machine + overhead rate for the work centre.
    cost_per_hour: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    #: Flat charge per run that is not time-based - a die change, a job-work
    #: piece rate, a plating bath minimum.
    fixed_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0"), server_default="0"
    )
    is_outsourced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "bom_id", "operation_sequence", name="uq_bom_operations_bom_id_operation_sequence"
        ),
        Index("ix_bom_operations_bom", "company_id", "bom_id", "operation_sequence"),
        CheckConstraint("operation_sequence > 0", name="operation_sequence_positive"),
        CheckConstraint("setup_time_minutes >= 0", name="setup_time_non_negative"),
        CheckConstraint("run_time_minutes_per_unit >= 0", name="run_time_non_negative"),
        CheckConstraint("cost_per_hour >= 0", name="cost_per_hour_non_negative"),
        CheckConstraint("fixed_cost >= 0", name="fixed_cost_non_negative"),
    )


__all__ = [
    "DEFAULT_MAX_EXPLOSION_DEPTH",
    "MAX_SCRAP_PERCENTAGE",
    "Bom",
    "BomLine",
    "BomOperation",
    "BomStatus",
    "BomType",
]
