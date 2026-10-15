"""Request and response schemas for bills of materials.

Conventions applied throughout, each for a specific reason:

* **Substitutes are addressed by line number, not by id.** On a create the lines
  do not have primary keys yet, so a payload that referenced ``UUID`` could only
  ever wire up substitutes in a second round-trip. The service resolves the
  number to the persisted id inside the same transaction.
* **``uom_id`` is optional and defaults to the component's stock unit.** The
  service rejects any other unit: multi-level consolidation adds quantities
  keyed on ``item_id``, and mixing units under one key produces a number that
  looks plausible and is wrong.
* **Money and quantities are ``Decimal`` with explicit precision.** Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (master rules
  section 84).
* **Costs are a separate response model**, returned only to a principal holding
  ``bom.cost_view``, so the value cannot leak through a schema someone forgot to
  trim.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.bom.models import BomStatus, BomType

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeQuantity = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
ScrapPercentage = Annotated[Decimal, Field(ge=0, lt=100, max_digits=9, decimal_places=4)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]

#: A BOM with more lines than this is a data-entry accident, not a product. The
#: bound matters because create and update both replace the whole line set in
#: one transaction.
MAX_LINES_PER_BOM = 500
MAX_OPERATIONS_PER_BOM = 100


# -----------------------------------------------------------------------------
# Lines
# -----------------------------------------------------------------------------


class BomLineIn(BaseModel):
    """One component on a create or update payload."""

    model_config = ConfigDict(extra="forbid")

    line_number: Annotated[int, Field(ge=1, le=100_000)]
    component_item_id: uuid.UUID
    quantity_per: PositiveQuantity
    #: Defaults to the component item's base unit. Supplying a different one is
    #: refused by the service rather than converted silently.
    uom_id: uuid.UUID | None = None
    scrap_percentage: ScrapPercentage = Decimal("0")
    is_optional: bool = False
    #: ``line_number`` of the line this one may replace, not its id.
    substitute_for_line_number: Annotated[int | None, Field(default=None, ge=1, le=100_000)] = None
    operation_sequence: Annotated[int | None, Field(default=None, ge=1)] = None
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _substitute_is_not_self(self) -> BomLineIn:
        if self.substitute_for_line_number == self.line_number:
            raise ValueError("A line cannot be a substitute for itself.")
        return self


class BomLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bom_id: uuid.UUID
    line_number: int
    component_item_id: uuid.UUID
    quantity_per: Decimal
    uom_id: uuid.UUID
    scrap_percentage: Decimal
    is_optional: bool
    substitute_for_line_id: uuid.UUID | None
    operation_sequence: int | None
    notes: str | None


# -----------------------------------------------------------------------------
# Operations
# -----------------------------------------------------------------------------


class BomOperationIn(BaseModel):
    """One costed routing step on a create or update payload."""

    model_config = ConfigDict(extra="forbid")

    operation_sequence: Annotated[int, Field(ge=1, le=100_000)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    description: str | None = Field(default=None, max_length=2000)
    #: Link back to ``routing_operations``/``work_centers``. Carried as a bare
    #: UUID because the routing module is built independently (see models.py).
    routing_operation_id: uuid.UUID | None = None
    work_center_id: uuid.UUID | None = None
    setup_time_minutes: NonNegativeQuantity = Decimal("0")
    run_time_minutes_per_unit: NonNegativeQuantity = Decimal("0")
    cost_per_hour: NonNegativeMoney = Decimal("0")
    fixed_cost: NonNegativeMoney = Decimal("0")
    is_outsourced: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class BomOperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bom_id: uuid.UUID
    operation_sequence: int
    name: str
    description: str | None
    routing_operation_id: uuid.UUID | None
    work_center_id: uuid.UUID | None
    setup_time_minutes: Decimal
    run_time_minutes_per_unit: Decimal
    cost_per_hour: Decimal
    fixed_cost: Decimal
    is_outsourced: bool
    notes: str | None


# -----------------------------------------------------------------------------
# BOM header
# -----------------------------------------------------------------------------


def _validate_line_set(lines: list[BomLineIn]) -> list[BomLineIn]:
    """Reject duplicate line numbers, duplicate components and dangling substitutes.

    Checked here rather than only in the database so the client gets one clear
    message naming the offending line, instead of an IntegrityError translated
    into a generic 409.
    """
    numbers = [line.line_number for line in lines]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        raise ValueError(f"Duplicate line numbers: {duplicates}")

    known = set(numbers)
    dangling = sorted(
        {
            line.substitute_for_line_number
            for line in lines
            if line.substitute_for_line_number is not None
            and line.substitute_for_line_number not in known
        }
    )
    if dangling:
        raise ValueError(f"Substitute lines reference unknown line numbers: {dangling}")

    # Two lines for the same component would be summed by the explosion into a
    # quantity nobody typed. A genuine second usage belongs on one line with the
    # combined quantity; a substitute is marked as one.
    primary = [line.component_item_id for line in lines if line.substitute_for_line_number is None]
    repeated = sorted({str(i) for i in primary if primary.count(i) > 1})
    if repeated:
        raise ValueError(f"An item appears on more than one primary line: {repeated}")

    return lines


def _validate_operation_set(operations: list[BomOperationIn]) -> list[BomOperationIn]:
    sequences = [op.operation_sequence for op in operations]
    duplicates = sorted({s for s in sequences if sequences.count(s) > 1})
    if duplicates:
        raise ValueError(f"Duplicate operation sequences: {duplicates}")
    return operations


class BomCreate(BaseModel):
    """Payload for a new BOM.

    ``company_id`` is absent by design - it comes from the authenticated
    principal (master rules section 40). ``code`` is absent too: it is issued
    from the document sequence so two engineers creating a BOM at the same
    moment cannot be given the same one.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    revision: str | None = Field(default=None, max_length=16)
    bom_type: BomType = BomType.PRODUCTION
    output_quantity: PositiveQuantity = Decimal("1")
    #: Defaults to the parent item's base unit; a different one is refused.
    base_uom_id: uuid.UUID | None = None
    #: Defaults to today. Backdating is allowed - a structure is often recorded
    #: after it went into use.
    effective_from: date | None = None
    effective_to: date | None = None
    parent_bom_id: uuid.UUID | None = None
    #: Only meaningful once approved; the partial unique index allows exactly one
    #: default approved BOM per item.
    is_default: bool = True
    notes: str | None = Field(default=None, max_length=4000)

    lines: Annotated[list[BomLineIn], Field(min_length=1, max_length=MAX_LINES_PER_BOM)]
    operations: Annotated[
        list[BomOperationIn], Field(default_factory=list, max_length=MAX_OPERATIONS_PER_BOM)
    ]

    @model_validator(mode="after")
    def _check(self) -> BomCreate:
        if (
            self.effective_to is not None
            and self.effective_from is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to cannot precede effective_from.")
        _validate_line_set(self.lines)
        _validate_operation_set(self.operations)
        if self.bom_type is BomType.ALTERNATE and self.parent_bom_id is None:
            raise ValueError("An ALTERNATE bill of materials must name the BOM it replaces.")
        return self


class BomUpdate(BaseModel):
    """Partial update of a DRAFT BOM.

    ``item_id`` and ``version`` are absent: both are identity. Re-pointing a BOM
    at a different item would break the composite foreign key that keeps
    ``bom_lines.parent_item_id`` honest, and a version is created by copying,
    never by editing.

    ``lines`` and ``operations`` are wholesale replacements when present. A
    per-line patch protocol would need its own concurrency story on top of the
    row version, for a screen that submits the whole grid anyway.
    """

    model_config = ConfigDict(extra="forbid")

    revision: str | None = Field(default=None, max_length=16)
    bom_type: BomType | None = None
    output_quantity: PositiveQuantity | None = None
    base_uom_id: uuid.UUID | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    parent_bom_id: uuid.UUID | None = None
    is_default: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)

    lines: Annotated[
        list[BomLineIn] | None, Field(default=None, min_length=1, max_length=MAX_LINES_PER_BOM)
    ] = None
    operations: Annotated[
        list[BomOperationIn] | None, Field(default=None, max_length=MAX_OPERATIONS_PER_BOM)
    ] = None

    @model_validator(mode="after")
    def _check(self) -> BomUpdate:
        if self.lines is not None:
            _validate_line_set(self.lines)
        if self.operations is not None:
            _validate_operation_set(self.operations)
        return self


class BomSummary(BaseModel):
    """List row. Deliberately without lines - the grid does not render them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    item_id: uuid.UUID
    version: int
    revision: str | None
    status: BomStatus
    bom_type: BomType
    is_default: bool
    output_quantity: Decimal
    base_uom_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    parent_bom_id: uuid.UUID | None
    approved_by_id: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BomDetail(BomSummary):
    """One BOM with its lines and operations, loaded in two batched queries."""

    notes: str | None = None
    lines: list[BomLineRead] = Field(default_factory=list)
    operations: list[BomOperationRead] = Field(default_factory=list)


class BomDetailWithCost(BomDetail):
    """Detail plus the stored roll-up.

    A separate model rather than nullable fields on :class:`BomDetail`: a
    principal without ``bom.cost_view`` receives a payload in which the cost
    fields do not exist at all.
    """

    total_material_cost: Decimal
    total_operation_cost: Decimal
    cost_rolled_up_at: datetime | None


class BomFilters(BaseModel):
    """List filters, validated before the repository sees them."""

    search: str | None = Field(default=None, max_length=120)
    item_id: uuid.UUID | None = None
    status: BomStatus | None = None
    bom_type: BomType | None = None
    is_default: bool | None = None
    #: Restrict to structures whose effectivity window contains this date.
    effective_on: date | None = None


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------


class BomStatusChange(BaseModel):
    """Reason capture for a lifecycle transition.

    Required, not optional: master rules section 83. A structure that went
    obsolete for no recorded reason is unanswerable six months later.
    """

    reason: Annotated[str, Field(min_length=3, max_length=255)]


class CopyAsNewVersionRequest(BaseModel):
    """Create the next version of a BOM from an existing one."""

    model_config = ConfigDict(extra="forbid")

    revision: str | None = Field(default=None, max_length=16)
    effective_from: date | None = None
    effective_to: date | None = None
    copy_operations: bool = True
    notes: str | None = Field(default=None, max_length=4000)
    reason: Annotated[str, Field(min_length=3, max_length=255)]


# -----------------------------------------------------------------------------
# Explosion / cost / where-used projections
# -----------------------------------------------------------------------------


class ExplodedComponentRead(BaseModel):
    """One consolidated requirement line."""

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    item_code: str
    item_name: str
    #: Consolidated across every branch it appears on, before scrap allowance.
    total_quantity: Decimal
    #: The same requirement with each branch's scrap allowance applied. This is
    #: the number to plan and purchase against.
    scrap_adjusted_quantity: Decimal
    uom_id: uuid.UUID
    #: Shallowest level at which the item appears; 1 is a direct component.
    level: int
    is_purchased: bool
    is_manufactured: bool
    #: Every path that contributed, so the merged quantity stays auditable.
    #: Capped at a fixed number of branches; ``source_path_count`` reports the
    #: true total so a truncated listing cannot be read as the whole story.
    source_paths: list[list[str]]
    source_path_count: int


class BomExplosionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    root_item_id: uuid.UUID
    root_item_code: str
    bom_id: uuid.UUID
    bom_code: str
    #: The status of the BOM that was exploded. Only APPROVED structures are
    #: used below the root, so anything else here means the caller explicitly
    #: asked for an in-progress structure.
    root_bom_status: BomStatus
    quantity: Decimal
    as_of: date
    max_level_reached: int
    #: Phantom items blown through: they raise no requirement themselves.
    phantom_item_codes: list[str]
    components: list[ExplodedComponentRead]


class BomCostComponentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    item_code: str
    #: Quantity of this component per one unit of the costed item's output.
    quantity_per_unit: Decimal
    unit_cost: Decimal
    extended_cost: Decimal
    #: Where the unit cost came from: rolled up from the component's own BOM, the
    #: item's standard cost, or the last purchase price.
    cost_source: str


class BomCostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    item_code: str
    bom_id: uuid.UUID
    bom_code: str
    output_quantity: Decimal
    material_cost: Decimal
    operation_cost: Decimal
    subcontract_operation_cost: Decimal
    total_cost: Decimal
    #: Cost of one unit of the item, i.e. ``total_cost / output_quantity``.
    unit_cost: Decimal
    #: Direct components with their rolled-up unit costs.
    components: list[BomCostComponentRead]
    #: Items anywhere in the structure with no cost on file. The roll-up treats
    #: them as zero and names them rather than reporting a total that quietly
    #: understates.
    items_without_cost: list[str]


class WhereUsedRead(BaseModel):
    """One parent that consumes the queried component."""

    model_config = ConfigDict(from_attributes=True)

    parent_item_id: uuid.UUID
    parent_item_code: str
    parent_item_name: str
    bom_id: uuid.UUID
    bom_code: str
    bom_version: int
    bom_status: BomStatus
    is_default: bool
    line_number: int
    quantity_per: Decimal
    #: 1 is a direct parent, 2 is a parent of a parent, and so on.
    level: int


__all__ = [
    "MAX_LINES_PER_BOM",
    "MAX_OPERATIONS_PER_BOM",
    "BomCostComponentRead",
    "BomCostRead",
    "BomCreate",
    "BomDetail",
    "BomDetailWithCost",
    "BomExplosionRead",
    "BomFilters",
    "BomLineIn",
    "BomLineRead",
    "BomOperationIn",
    "BomOperationRead",
    "BomStatusChange",
    "BomSummary",
    "BomUpdate",
    "CopyAsNewVersionRequest",
    "ExplodedComponentRead",
    "WhereUsedRead",
]
