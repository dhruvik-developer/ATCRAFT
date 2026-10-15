"""Quality control: what gets inspected, against which specification, and what
happened to the stock afterwards.

Master rules section 21. Five modelling decisions carry this file.

**THE RULE THIS MODULE EXISTS FOR: an inspection decides where held stock
goes.** Goods arriving against a receipt that requires inspection are posted by
the receiving module straight into ``inventory_balances.qc_hold_quantity`` -
present, counted, valued, and promisable to nobody. They stay there until an
inspection says otherwise. Deciding an inspection is therefore not a status
update with a stock update bolted on: it is a stock movement out of ``QC_HOLD``
(to ``AVAILABLE``, ``DAMAGED`` or ``SCRAP``) that happens to record a verdict at
the same time, in one transaction, with one audit entry. A decision that
committed without moving the quantity would leave material nobody can use and
nobody can find; a movement that committed without the decision would release
stock nothing vouched for.

**Creating an inspection never holds stock by itself.** The goods receipt, the
job work return and the assembly completion each post their own ``QC_HOLD``
movement, because only they know the batch, the cost and the ledger reference of
what arrived. This module holding it a second time would double the held
quantity. The one exception is an inspection raised by hand off the shelf, which
has no upstream document to have done it - see ``service.create_inspection``.

**A template is versioned, never edited after use.** ``qc_inspection_results``
points at a ``qc_template_parameters`` row, so changing a tolerance in place
would silently restate every past measurement's pass or fail. Once a template
has been applied to an inspection its specification is frozen and a change forks
a new ``version``; ``qc_inspections.template_version`` records which revision was
actually applied.

**Exactly one active template resolves for a given item and stage.** Three
partial unique indexes enforce it: one active template per (item, stage), one per
(category, stage), and one stage-wide default. Resolution is then a lookup with a
deterministic answer rather than an ``ORDER BY`` whose result depends on
insertion order.

**``result`` and ``status`` are different questions.** ``status`` is where the
document is in its lifecycle (database.md section 7); ``result`` is the verdict on
the goods. They are kept in step by a check constraint - an inspection that is
still ``PENDING`` cannot carry a verdict, and one that has been decided or closed
cannot lack one - because the two are read by different screens and a drift
between them is invisible until somebody trusts the wrong one.
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
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import ImmutableLedgerEntity, TenantEntity, TenantMasterEntity, sql_in_list
from src.core.types import GUID, Money, Percentage, Quantity

# Shared VARCHAR-backed status type. It stores exactly what a plain
# ``String`` column would - the DDL is unchanged - but reads back as the
# StrEnum member, so ``inspection.status is InspectionStatus.PENDING`` means
# the same thing for an object in memory and one loaded from the database.
# Without it the two disagree, and the bug only surfaces on the second
# request. ``allocation`` and ``mrp`` import it from the same place.
from src.modules.inventory.models import StatusEnum

#: Measured values and their tolerances get six decimal places, not the four of
#: ``Quantity``. A plating thickness specified as 2.5 microns +/- 0.25 and a bore
#: held to 0.0005 mm are ordinary hardware tolerances; rounding a measurement to
#: four places would make a part pass a specification it actually misses.
#: Deliberately neither ``Money`` nor ``Quantity`` - this is metrology, and
#: borrowing a money scale for it would be a lie about what the column means.
MeasurementValue = Numeric(18, 6)

#: Longest parameter name a template may carry. Long enough for
#: "PVD coating thickness - outer face", short enough to print on a check sheet.
PARAMETER_NAME_LENGTH = 120

#: Longest single entry in ``allowed_values``, and the widest text measurement.
#: A SELECT parameter names a grade or a finish, not a paragraph; a paragraph
#: belongs in ``remark``.
ALLOWED_VALUE_LENGTH = 64
TEXT_MEASUREMENT_LENGTH = 255

#: An inspection form with more parameters than this is a document, not a check
#: sheet. The bound matters because recording results validates and writes every
#: parameter of the template inside one locked transaction.
MAX_PARAMETERS_PER_TEMPLATE = 200


class QcStage(StrEnum):
    """Where in the flow the inspection happens.

    A classification of *when*, not of what is permitted: no rule reads this to
    decide whether an operation is legal. It selects the template, groups the
    pending queue by the team that works it, and is the axis the quality reports
    slice on.
    """

    INCOMING = "INCOMING"
    JOB_WORK = "JOB_WORK"
    IN_PROCESS = "IN_PROCESS"
    ASSEMBLY = "ASSEMBLY"
    FINAL = "FINAL"
    RETURN = "RETURN"


class SamplingPlan(StrEnum):
    """How much of a delivered lot is actually measured.

    ``ALL``            every piece. The default when nothing else is configured.
    ``PERCENTAGE``     a proportion of the lot, rounded up, never below one.
    ``FIXED_SAMPLE``   a constant number of pieces whatever the lot size.
    ``AQL``            ISO 2859-1 general inspection level II: the sample grows
                       with the lot but far more slowly than a percentage, which
                       is the entire point of a statistical plan.
    """

    ALL = "ALL"
    PERCENTAGE = "PERCENTAGE"
    FIXED_SAMPLE = "FIXED_SAMPLE"
    AQL = "AQL"


class ParameterType(StrEnum):
    """What kind of value a parameter records, and therefore how it is judged.

    The type decides which ``measured_value_*`` column carries the reading and
    which specification columns are meaningful: a tolerance band belongs to a
    NUMERIC parameter and nothing else, and an ``allowed_values`` list belongs to
    a SELECT. Storing a number as text would make every range check a string
    comparison, which is how "10" comes out smaller than "9".
    """

    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    TEXT = "TEXT"
    SELECT = "SELECT"


class InspectionResult(StrEnum):
    """The verdict on the goods.

    ``PENDING`` until a decision is taken. The other five each have exactly one
    stock consequence, applied by ``service.decide_inspection``:

    ``ACCEPTED``  released from QC hold into the available pool.
    ``REWORK``    stays on QC hold, flagged for rework, re-decided afterwards.
    ``REJECTED``  moved to the damaged pool: present and counted, promisable to
                  nobody, and awaiting a return or a replacement.
    ``HOLD``      stays on QC hold, undecided, waiting for someone with more
                  authority or more information.
    ``SCRAP``     written off to the scrap pool. It never comes back.
    """

    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REWORK = "REWORK"
    REJECTED = "REJECTED"
    HOLD = "HOLD"
    SCRAP = "SCRAP"


class InspectionStatus(StrEnum):
    """Lifecycle of the inspection document (database.md section 7).

    The five decision states duplicate :class:`InspectionResult` on purpose:
    ``status`` answers "what is left to do with this document", ``result``
    answers "what did we conclude about the goods". A check constraint keeps the
    two consistent. ``HOLD`` and ``REWORK`` are deliberately not terminal - stock
    parked in either still sits in the QC hold pool and has to be re-decided
    before it can leave, and a status graph that let it stay there forever would
    be modelling the bug rather than the process.
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    ACCEPTED = "ACCEPTED"
    REWORK = "REWORK"
    REJECTED = "REJECTED"
    HOLD = "HOLD"
    SCRAP = "SCRAP"
    CLOSED = "CLOSED"


#: Statuses in which the inspection has been decided but the document is still
#: open. The pending queue excludes them; the rework and hold work lists are
#: built from them.
DECIDED_STATUSES: tuple[InspectionStatus, ...] = (
    InspectionStatus.ACCEPTED,
    InspectionStatus.REWORK,
    InspectionStatus.REJECTED,
    InspectionStatus.HOLD,
    InspectionStatus.SCRAP,
)

#: Statuses in which somebody still owes this inspection a verdict. The queue
#: endpoint and the partial index behind it are built for exactly this set, so
#: the tuple and the index have to change together.
OPEN_STATUSES: tuple[InspectionStatus, ...] = (
    InspectionStatus.PENDING,
    InspectionStatus.IN_PROGRESS,
)

#: Decided statuses whose quantity is still sitting in the QC hold pool, and can
#: therefore still be re-decided out of it.
REOPENABLE_STATUSES: tuple[InspectionStatus, ...] = (
    InspectionStatus.REWORK,
    InspectionStatus.HOLD,
)


class QcTemplate(TenantMasterEntity):
    """A reusable inspection specification: what to measure, and how much to look at.

    Master data, so soft-deleted rather than removed: an inspection carried out
    six months ago names the template it followed, and a retired template that
    vanished would take the meaning of that record with it.
    """

    __tablename__ = "qc_templates"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: What the template applies to, in narrowing order of preference. Both NULL
    #: makes it the stage-wide default. Both set is refused by the check
    #: constraint below: a template claiming to be both the rule for one item and
    #: the rule for a whole category would resolve differently depending on which
    #: index the planner reached for.
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=True
    )
    item_category_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("item_categories.id", ondelete="RESTRICT"), nullable=True
    )

    qc_stage: Mapped[QcStage] = mapped_column(StatusEnum(QcStage), nullable=False)

    sampling_plan: Mapped[SamplingPlan] = mapped_column(
        StatusEnum(SamplingPlan, 16),
        nullable=False,
        default=SamplingPlan.ALL,
        server_default=SamplingPlan.ALL.value,
    )
    #: Proportion of the lot to measure, for ``PERCENTAGE``. A percentage rather
    #: than a fraction because that is how an inspection instruction is written.
    sample_percentage: Mapped[Decimal | None] = mapped_column(Percentage, nullable=True)
    #: Constant piece count, for ``FIXED_SAMPLE``. An integer: half a piece
    #: cannot be inspected.
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    #: Revision number of this specification, NOT an optimistic-lock counter.
    #: A template that has been applied to an inspection is frozen; changing a
    #: tolerance forks version n+1 and retires version n, so the pass or fail of
    #: every past measurement stays reproducible against the specification it was
    #: judged by. This is why the natural key below is (code, version) rather
    #: than the (company, code) the conventions use for a document number.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint(
            "company_id", "code", "version", name="uq_qc_templates_company_id_code_version"
        ),
        # Exactly one active template may claim each scope, so resolution has a
        # deterministic answer rather than one that depends on insertion order.
        Index(
            "uq_qc_templates_active_item_stage",
            "company_id",
            "item_id",
            "qc_stage",
            unique=True,
            postgresql_where=text("is_active AND deleted_at IS NULL AND item_id IS NOT NULL"),
        ),
        Index(
            "uq_qc_templates_active_category_stage",
            "company_id",
            "item_category_id",
            "qc_stage",
            unique=True,
            postgresql_where=text(
                "is_active AND deleted_at IS NULL AND item_category_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_qc_templates_active_stage_default",
            "company_id",
            "qc_stage",
            unique=True,
            postgresql_where=text(
                "is_active AND deleted_at IS NULL AND item_id IS NULL AND item_category_id IS NULL"
            ),
        ),
        # The template picker, and the covering index for resolution.
        Index("ix_qc_templates_stage", "company_id", "qc_stage", "is_active"),
        CheckConstraint(
            "item_id IS NULL OR item_category_id IS NULL", name="scope_is_item_or_category"
        ),
        CheckConstraint(f"qc_stage IN ({sql_in_list(tuple(QcStage))})", name="qc_stage_vocabulary"),
        CheckConstraint(
            f"sampling_plan IN ({sql_in_list(tuple(SamplingPlan))})",
            name="sampling_plan_vocabulary",
        ),
        # A plan without the number it needs is not a plan. Enforced here rather
        # than only in the request schema because templates are also written by
        # seeding and by data migration, neither of which goes through Pydantic.
        CheckConstraint(
            "sampling_plan <> 'PERCENTAGE' "
            "OR (sample_percentage IS NOT NULL AND sample_percentage > 0 "
            "    AND sample_percentage <= 100)",
            name="percentage_plan_needs_percentage",
        ),
        CheckConstraint(
            "sampling_plan <> 'FIXED_SAMPLE' OR (sample_size IS NOT NULL AND sample_size >= 1)",
            name="fixed_plan_needs_sample_size",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
    )


class QcTemplateParameter(TenantEntity):
    """One measurable characteristic on a template's check sheet.

    ``is_critical`` is the column with teeth: a critical parameter out of
    specification rejects the whole inspection whatever every other reading says
    (see ``service.evaluate_inspection``). It is what separates "the finish is
    slightly off" from "the thread does not engage".
    """

    __tablename__ = "qc_template_parameters"

    #: CASCADE: a parameter has no existence away from its template. Deleting a
    #: template that has been *used* is impossible anyway - it is soft-deleted -
    #: and a parameter with recorded measurements is protected by the RESTRICT on
    #: ``qc_inspection_results.parameter_id``.
    template_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("qc_templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    parameter_name: Mapped[str] = mapped_column(String(PARAMETER_NAME_LENGTH), nullable=False)
    parameter_type: Mapped[ParameterType] = mapped_column(
        StatusEnum(ParameterType, 16), nullable=False
    )
    #: Unit the reading is expressed in - "mm", "micron", "N". Free text: the
    #: units-of-measure master is about stock-keeping units, not metrology, and
    #: forcing a foreign key onto it would make "shore A hardness" unrecordable.
    unit: Mapped[str | None] = mapped_column(String(24), nullable=True)

    min_value: Mapped[Decimal | None] = mapped_column(MeasurementValue, nullable=True)
    max_value: Mapped[Decimal | None] = mapped_column(MeasurementValue, nullable=True)
    #: The nominal figure, for the operator's benefit and for drift reporting.
    #: Never a pass/fail rule on its own - a target with no tolerance band cannot
    #: be met by a real measurement.
    target_value: Mapped[Decimal | None] = mapped_column(MeasurementValue, nullable=True)

    #: The closed vocabulary a SELECT parameter accepts. Also honoured for TEXT,
    #: where it is optional: a free-text note stays free unless a list is given.
    allowed_values: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(ALLOWED_VALUE_LENGTH)), nullable=True
    )

    is_critical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: A mandatory parameter must carry a reading before the inspection can be
    #: decided. An optional one may be skipped - a destructive test is not run on
    #: every lot.
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    __table_args__ = (
        UniqueConstraint(
            "template_id", "sequence", name="uq_qc_template_parameters_template_id_sequence"
        ),
        # The check sheet, in the order it is worked.
        Index("ix_qc_template_parameters_order", "company_id", "template_id", "sequence"),
        CheckConstraint(
            f"parameter_type IN ({sql_in_list(tuple(ParameterType))})",
            name="parameter_type_vocabulary",
        ),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "min_value IS NULL OR max_value IS NULL OR min_value <= max_value",
            name="min_not_above_max",
        ),
        # A tolerance band on a boolean or a text check is meaningless, and one
        # that existed would be silently ignored by the evaluator - worse than
        # being refused, because the template would look configured.
        CheckConstraint(
            "parameter_type = 'NUMERIC' "
            "OR (min_value IS NULL AND max_value IS NULL AND target_value IS NULL)",
            name="bounds_are_numeric_only",
        ),
        CheckConstraint(
            "parameter_type <> 'SELECT' "
            "OR (allowed_values IS NOT NULL AND cardinality(allowed_values) > 0)",
            name="select_needs_allowed_values",
        ),
        CheckConstraint(
            "parameter_type IN ('SELECT', 'TEXT') OR allowed_values IS NULL",
            name="allowed_values_are_textual_only",
        ),
    )


class QcInspection(TenantEntity):
    """One inspection of one lot: what was looked at, what was decided, what moved.

    No optimistic-lock version. Deciding an inspection moves stock, so it is
    serialised the way every other stock operation is - ``SELECT ... FOR UPDATE``
    on this row before anything is read or written (database.md section 4.4). A
    version counter would report the conflict *after* two decisions had both
    computed their movements from the same stale quantities.
    """

    __tablename__ = "qc_inspections"

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    qc_stage: Mapped[QcStage] = mapped_column(StatusEnum(QcStage), nullable=False)

    item_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    #: Where the held quantity physically is. Every movement this inspection
    #: posts is against this position, so it is fixed at creation.
    warehouse_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    #: The lot inspected. Required in practice for a batch-tracked item, because
    #: the inventory engine will not move one without it; the column is nullable
    #: because most items are not batch tracked.
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("stock_batches.id", ondelete="RESTRICT"), nullable=True
    )

    #: The specification applied. Nullable: an inspection can be raised for a
    #: part nobody has written a check sheet for yet, and refusing to raise it
    #: would mean the stock is never inspected at all rather than inspected
    #: against the inspector's own judgement.
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("qc_templates.id", ondelete="RESTRICT"), nullable=True
    )
    #: Frozen copy of ``qc_templates.version`` at the moment the inspection was
    #: raised, so a report can say which revision of the specification a reading
    #: was judged against without walking the template's history.
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Who supplied the goods, for incoming inspection. Denormalised from the
    #: receipt rather than joined through it because the vendor rating engine
    #: aggregates rejection rates straight off this table
    #: (``vendors.service.load_performance`` reads exactly ``company_id``,
    #: ``vendor_id``, ``inspected_at``, ``inspected_quantity`` and
    #: ``rejected_quantity``), and that read must not depend on the procurement
    #: module being deployed.
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True
    )

    #: What triggered the inspection - the GRN, job work receipt, production
    #: order or assembly order. A bare id with a type beside it, not a foreign
    #: key: the trigger lives in one of several tables and the inspection has to
    #: outlive it, exactly as the inventory ledger does (database.md section 4.2).
    reference_type: Mapped[str] = mapped_column(String(48), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    reference_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: The size of the lot the verdict applies to. Every disposition quantity is
    #: a share of this, and together they must account for all of it.
    inspected_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: How many pieces were actually measured, from the template's sampling plan.
    #: Recorded because "we accepted 5,000 on the strength of 32" is the sentence
    #: an audit needs to be able to reconstruct.
    sample_size: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    accepted_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rejected_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    rework_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    hold_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )
    scrap_quantity: Mapped[Decimal] = mapped_column(
        Quantity, nullable=False, default=Decimal("0"), server_default="0"
    )

    result: Mapped[InspectionResult] = mapped_column(
        StatusEnum(InspectionResult, 16),
        nullable=False,
        default=InspectionResult.PENDING,
        server_default=InspectionResult.PENDING.value,
    )
    status: Mapped[InspectionStatus] = mapped_column(
        StatusEnum(InspectionStatus),
        nullable=False,
        default=InspectionStatus.PENDING,
        server_default=InspectionStatus.PENDING.value,
    )

    #: Why material was refused, in the inspector's words. Mandatory whenever
    #: anything was rejected or scrapped - a rejection with no stated cause is
    #: unusable for vendor feedback, for a debit note and for a corrective
    #: action, and section 83 requires a reason for every stock event anyway.
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    inspector_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Business time of the inspection, which is not the row's ``created_at``: an
    #: inspection carried out on the shop floor at 18:00 may be keyed in the next
    #: morning, and every quality trend is plotted on this.
    inspected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set when a rejection still owes the vendor a return or a replacement, and
    #: cleared when procurement raises the purchase return. A flag rather than a
    #: document created from here because the return is procurement's decision,
    #: its numbering series and its approval - see ``service.decide_inspection``.
    return_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_qc_inspections_company_id_code"),
        # The pending queue: what is waiting to be inspected, oldest first. A
        # partial index because the queue is a small slice of a table that grows
        # with every receipt, forever.
        Index(
            "ix_qc_inspections_open_queue",
            "company_id",
            "qc_stage",
            "created_at",
            postgresql_where=text(f"status IN ({sql_in_list(OPEN_STATUSES)})"),
        ),
        # Product quality history: rejection rate for one item over a window.
        Index("ix_qc_inspections_item_history", "company_id", "item_id", "inspected_at"),
        # Vendor quality report, and the vendor rating engine's aggregate.
        Index(
            "ix_qc_inspections_vendor_history",
            "company_id",
            "vendor_id",
            "inspected_at",
            postgresql_where=text("vendor_id IS NOT NULL"),
        ),
        # "Which inspection did this GRN raise?" - the receipt's own screen.
        Index("ix_qc_inspections_reference", "company_id", "reference_type", "reference_id"),
        # The list endpoint's default filter pair.
        Index("ix_qc_inspections_result", "company_id", "result", "created_at"),
        # The rework bench's work list: quantity still on QC hold that somebody
        # has been asked to put right.
        Index(
            "ix_qc_inspections_rework",
            "company_id",
            "warehouse_id",
            "item_id",
            postgresql_where=text("rework_quantity > 0 AND status <> 'CLOSED'"),
        ),
        CheckConstraint(f"qc_stage IN ({sql_in_list(tuple(QcStage))})", name="qc_stage_vocabulary"),
        CheckConstraint(
            f"status IN ({sql_in_list(tuple(InspectionStatus))})", name="status_vocabulary"
        ),
        CheckConstraint(
            f"result IN ({sql_in_list(tuple(InspectionResult))})", name="result_vocabulary"
        ),
        CheckConstraint("inspected_quantity > 0", name="inspected_quantity_positive"),
        CheckConstraint(
            "sample_size > 0 AND sample_size <= inspected_quantity", name="sample_within_lot"
        ),
        CheckConstraint("accepted_quantity >= 0", name="accepted_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("rework_quantity >= 0", name="rework_non_negative"),
        CheckConstraint("hold_quantity >= 0", name="hold_non_negative"),
        CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
        # No disposition may exceed the lot at any point in the document's life,
        # not only at close: an intermediate state that over-disposes has already
        # posted movements for stock that was never held.
        CheckConstraint(
            "accepted_quantity + rejected_quantity + rework_quantity "
            "+ hold_quantity + scrap_quantity <= inspected_quantity",
            name="disposition_within_lot",
        ),
        # ... and by the time it closes, every piece must be accounted for.
        # Quantity belonging to no disposition is stock sitting on QC hold with a
        # closed document that explains nothing.
        CheckConstraint(
            "status <> 'CLOSED' "
            "OR accepted_quantity + rejected_quantity + rework_quantity "
            "   + hold_quantity + scrap_quantity = inspected_quantity",
            name="closed_disposition_balances",
        ),
        # status and result answer different questions, but may never disagree
        # about whether a decision has been taken at all.
        CheckConstraint(
            f"(status IN ({sql_in_list(OPEN_STATUSES)}) AND result = 'PENDING') "
            f"OR (status NOT IN ({sql_in_list(OPEN_STATUSES)}) AND result <> 'PENDING')",
            name="status_and_result_agree",
        ),
        CheckConstraint(
            "rejected_quantity + scrap_quantity = 0 OR rejection_reason IS NOT NULL",
            name="refusal_states_a_reason",
        ),
        # A decided inspection has an inspector and a time. Without them the
        # quality history is a set of numbers nobody signed.
        CheckConstraint(
            f"status IN ({sql_in_list(OPEN_STATUSES)}) "
            "OR (inspector_user_id IS NOT NULL AND inspected_at IS NOT NULL)",
            name="decision_is_attributed",
        ),
        CheckConstraint(
            "template_id IS NOT NULL OR template_version IS NULL", name="version_needs_template"
        ),
    )


class ReworkStatus(StrEnum):
    """Lifecycle of one rework cycle (database.md section 7).

    ``OPEN -> IN_PROGRESS -> DONE``. Completing the cycle re-opens the
    inspection for re-decision; the cycle itself never reopens, because a
    second round of work is a second record, not a reopened one.
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class ReworkOutcome(StrEnum):
    """What the bench did with the rework quantity.

    ``REWORKED_OK`` goes back for re-inspection; ``SCRAPPED`` writes the
    quantity off; ``RETURNED`` sends it back to the vendor. Recorded at
    completion, never before - an outcome on an unfinished cycle is a guess.
    """

    REWORKED_OK = "REWORKED_OK"
    SCRAPPED = "SCRAPPED"
    RETURNED = "RETURNED"


class QcReworkRecord(TenantEntity):
    """One rework cycle against one inspection: who did what, where, for how much.

    The inspection's ``rework_quantity`` says how much is on the bench; this row
    says what the bench is doing about it. Stock stays in the QC hold pool for
    the whole cycle - no movement is posted here - and completing the record
    re-opens the inspection so the reworked quantity is re-decided.
    """

    __tablename__ = "qc_rework_records"

    code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: CASCADE: the cycle is part of the inspection's history. An inspection is
    #: never deleted once decided, so in practice this fires only when a
    #: still-pending inspection is discarded.
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("qc_inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: How much of the inspection's rework quantity this cycle covers.
    quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    #: Who is doing the work, or where. One of the two is expected; both set
    #: means the assignee works at that centre, which is legitimate.
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    work_center_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("work_centers.id", ondelete="RESTRICT"), nullable=True
    )

    status: Mapped[ReworkStatus] = mapped_column(
        StatusEnum(ReworkStatus, 16),
        nullable=False,
        default=ReworkStatus.OPEN,
        server_default=ReworkStatus.OPEN.value,
    )
    outcome: Mapped[ReworkOutcome | None] = mapped_column(
        StatusEnum(ReworkOutcome, 16), nullable=True
    )
    #: What the rework cost, when it is tracked. Optional money, so Decimal and
    #: NUMERIC like every other amount.
    cost: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_qc_rework_records_company_id_code"),
        # The inspection's cycles, oldest first, and the bench's work list.
        Index("ix_qc_rework_records_inspection", "company_id", "inspection_id", "created_at"),
        Index(
            "ix_qc_rework_records_open",
            "company_id",
            "status",
            postgresql_where=text("status <> 'DONE'"),
        ),
        CheckConstraint(f"status IN ({sql_in_list(tuple(ReworkStatus))})", name="status_known"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({sql_in_list(tuple(ReworkOutcome))})",
            name="outcome_known",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost_non_negative"),
        # An outcome is the completion's verdict on the cycle: nothing unfinished
        # carries one, and nothing finished lacks one.
        CheckConstraint(
            "(status = 'DONE') = (outcome IS NOT NULL)", name="outcome_matches_completion"
        ),
        CheckConstraint(
            "(status = 'DONE') = (completed_at IS NOT NULL)", name="completion_is_timed"
        ),
    )


class QcInspectionDecision(ImmutableLedgerEntity):
    """One verdict in an inspection's decision history.

    Every call to ``decide_inspection`` appends a row here with the cumulative
    disposition it wrote, so re-deciding a reworked lot does not erase how it
    was decided before. Append-only: a later decision never edits an earlier
    one, it only follows it (``sequence`` orders the cycles per inspection).
    """

    __tablename__ = "qc_inspection_decisions"

    #: CASCADE for the same reason as the rework record: history belongs to its
    #: inspection.
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("qc_inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 1, 2, 3 ... per inspection. Assigned by the service under the
    #: inspection's row lock, so two concurrent decisions cannot claim one number.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    result: Mapped[InspectionResult] = mapped_column(
        StatusEnum(InspectionResult, 16), nullable=False
    )
    accepted_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    rejected_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    rework_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    hold_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)
    scrap_quantity: Mapped[Decimal] = mapped_column(Quantity, nullable=False)

    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Business time of the verdict, matching the inspection's own ``inspected_at``.
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "inspection_id", "sequence", name="uq_qc_inspection_decisions_inspection_sequence"
        ),
        Index("ix_qc_inspection_decisions_inspection", "company_id", "inspection_id", "sequence"),
        CheckConstraint(f"result IN ({sql_in_list(tuple(InspectionResult))})", name="result_known"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("accepted_quantity >= 0", name="accepted_non_negative"),
        CheckConstraint("rejected_quantity >= 0", name="rejected_non_negative"),
        CheckConstraint("rework_quantity >= 0", name="rework_non_negative"),
        CheckConstraint("hold_quantity >= 0", name="hold_non_negative"),
        CheckConstraint("scrap_quantity >= 0", name="scrap_non_negative"),
    )


class QcInspectionResult(TenantEntity):
    """One measurement: what a parameter read on this inspection, and whether it passed.

    ``is_within_spec`` is stored rather than recomputed on read. The
    specification it was judged against can be superseded by a new template
    version, and a report that re-evaluated old readings against today's
    tolerances would quietly rewrite history.
    """

    __tablename__ = "qc_inspection_results"

    #: CASCADE: the measurements are part of the inspection, not records in their
    #: own right. An inspection is never deleted once decided, so in practice
    #: this fires only when a still-pending inspection is discarded.
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("qc_inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: RESTRICT: a parameter that has been measured cannot be deleted out from
    #: under the reading. This is what forces a template edit to fork a version
    #: instead of mutating a specification somebody has already been judged by.
    parameter_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("qc_template_parameters.id", ondelete="RESTRICT"), nullable=False
    )

    measured_value_numeric: Mapped[Decimal | None] = mapped_column(MeasurementValue, nullable=True)
    measured_value_text: Mapped[str | None] = mapped_column(
        String(TEXT_MEASUREMENT_LENGTH), nullable=True
    )
    measured_value_bool: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    is_within_spec: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Evidence photograph. A bare UUID with no foreign key, matching
    #: ``companies.logo_document_id``: the documents module applies its own
    #: retention, and a purged photograph must neither take the measurement with
    #: it nor block the purge.
    photo_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)

    __table_args__ = (
        # One reading per parameter per inspection. A second reading is a
        # correction of the first, applied as an update - two rows would make
        # "did it pass?" ambiguous.
        UniqueConstraint(
            "inspection_id",
            "parameter_id",
            name="uq_qc_inspection_results_inspection_id_parameter_id",
        ),
        # The check sheet as recorded, and the failure scan behind the verdict.
        Index("ix_qc_inspection_results_sheet", "company_id", "inspection_id"),
        # Parameter-level trend: "how often does plating thickness fail?"
        Index("ix_qc_inspection_results_parameter", "company_id", "parameter_id", "is_within_spec"),
        # Exactly one value column carries the reading. A row with none is not a
        # measurement, and a row with two cannot be judged: the evaluator would
        # have to guess which one the parameter's type meant.
        CheckConstraint(
            "num_nonnulls(measured_value_numeric, measured_value_text, measured_value_bool) = 1",
            name="exactly_one_measured_value",
        ),
    )


__all__ = [
    "ALLOWED_VALUE_LENGTH",
    "DECIDED_STATUSES",
    "MAX_PARAMETERS_PER_TEMPLATE",
    "OPEN_STATUSES",
    "PARAMETER_NAME_LENGTH",
    "REOPENABLE_STATUSES",
    "TEXT_MEASUREMENT_LENGTH",
    "InspectionResult",
    "InspectionStatus",
    "MeasurementValue",
    "ParameterType",
    "QcInspection",
    "QcInspectionDecision",
    "QcInspectionResult",
    "QcReworkRecord",
    "QcStage",
    "QcTemplate",
    "QcTemplateParameter",
    "ReworkOutcome",
    "ReworkStatus",
    "SamplingPlan",
]
