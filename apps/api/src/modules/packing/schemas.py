"""Request and response schemas for packing orders, cartons and labels.

Conventions applied throughout, each for a specific reason:

* **``company_id`` appears nowhere.** It comes from the authenticated principal
  through ``TenantContext``, never from a body or a query string (section 40).
* **Barcodes are never accepted from a client.** They are generated on the
  server from the order code and the carton number, so two boxes cannot be given
  the same scan and a client cannot mint one that collides with a future carton.
* **Carton contents name a line by its ``line_number``,** not by a primary key.
  The number is what the packing list prints and what a packer reads off the
  paper; resolving it server-side also means a content row can never be attached
  to a line belonging to somebody else's order.
* **Quantities and weights are ``Decimal`` with explicit precision**, so Pydantic
  rejects a float before it can reach a ``NUMERIC`` column (section 84).
* **Every irreversible action carries a mandatory reason.** Sealing consumes
  packing material, and section 83 requires a reason for every stock event.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.packing.models import (
    MAX_CARTONS_PER_ORDER,
    MAX_CONTENTS_PER_CARTON,
    MAX_LINES_PER_ORDER,
    CartonType,
    PackingOrderStatus,
)

# --- reusable constrained scalars --------------------------------------------

PositiveQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
NonNegativeWeight = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
PositiveDimension = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
Reason = Annotated[str, Field(min_length=3, max_length=255)]
Notes = Annotated[str, Field(max_length=2000)]
LineNumber = Annotated[int, Field(ge=1, le=100_000)]

#: Upper bound on a single ``ready-for-dispatch`` handover read. The
#: service-level contract the dispatch module calls returns a plain list, so it
#: needs a ceiling of its own - an unbounded result set is forbidden however it
#: is reached (section 31).
READY_FOR_DISPATCH_MAX = 200

#: Upper bound on a serial set in one content row. A carton holding more
#: individually-numbered units than this is a pallet, not a box, and the label
#: could not print them.
MAX_SERIALS_PER_CONTENT = 10_000


# -----------------------------------------------------------------------------
# Order header and lines
# -----------------------------------------------------------------------------


class PackingOrderLineIn(BaseModel):
    """One item to be boxed.

    ``sales_order_line_id`` is optional even on an order raised against a sales
    order: a shipment often includes something the customer did not order on
    that line - a replacement part, a sample - and forcing a link would either
    block it or invent a demand row for it.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    quantity_to_pack: PositiveQuantity
    uom_id: uuid.UUID
    sales_order_line_id: uuid.UUID | None = None


class PackingOrderCreate(BaseModel):
    """Raise a draft packing order."""

    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    planned_date: date
    #: The demand behind this shipment. Omitted for a sample, a warranty
    #: replacement or anything else packed without an order.
    sales_order_id: uuid.UUID | None = None
    notes: Notes | None = None
    lines: list[PackingOrderLineIn] = Field(min_length=1, max_length=MAX_LINES_PER_ORDER)


class PackingFromAssemblyCreate(BaseModel):
    """Raise a draft packing order for a completed assembly's output.

    Everything the assembly already knows is taken from it: the item, the
    quantity it produced and the warehouse that output sits in. The customer
    and sales order come from the assembly's source when it was raised for a
    sales order; otherwise the customer has to be named here.
    """

    model_config = ConfigDict(extra="forbid")

    assembly_order_id: uuid.UUID
    #: Required unless the assembly was raised for a sales order, whose customer
    #: is then used. When given for such an assembly it must be that customer.
    customer_id: uuid.UUID | None = None
    #: Defaults to the assembly's source sales order, when it has one.
    sales_order_id: uuid.UUID | None = None
    #: Defaults to today.
    planned_date: date | None = None
    #: Defaults to everything the assembly produced; may be less, never more.
    quantity: PositiveQuantity | None = None
    notes: Notes | None = None


class PackingOrderUpdate(BaseModel):
    """Amend a draft.

    Only a DRAFT is editable: from IN_PROGRESS the bench has boxes open against
    these lines, and from PACKED the material has been consumed. ``customer_id``
    and ``sales_order_id`` are absent on purpose - shipping to a different
    customer is a different order, not an edit of this one.
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: uuid.UUID | None = None
    planned_date: date | None = None
    notes: Notes | None = None
    #: Supplying this replaces the whole line set. Patching lines individually
    #: would need a line identity in the payload, and a half-applied edit would
    #: leave the order asking for a quantity nobody decided on.
    lines: list[PackingOrderLineIn] | None = Field(
        default=None, min_length=1, max_length=MAX_LINES_PER_ORDER
    )


class PackingOrderLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    packing_order_id: uuid.UUID
    line_number: int
    sales_order_line_id: uuid.UUID | None
    item_id: uuid.UUID
    quantity_to_pack: Decimal
    packed_quantity: Decimal
    uom_id: uuid.UUID
    #: The FINAL inspection starting the order raised for this line, when its
    #: item requires final QC. The order cannot complete or hand over until it
    #: is ACCEPTED; a REJECTED one is replaced through re-inspection.
    qc_inspection_id: uuid.UUID | None = None


class PackingOrderSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    sales_order_id: uuid.UUID | None
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    status: PackingOrderStatus
    planned_date: date
    packed_date: datetime | None
    packed_by_id: uuid.UUID | None
    total_cartons: int
    total_gross_weight: Decimal
    total_net_weight: Decimal
    dispatch_id: uuid.UUID | None
    handed_over_at: datetime | None
    notes: str | None
    version: int


class PackingOrderDetail(PackingOrderSummary):
    """Header plus its line set.

    Lines are not paginated, by deliberate exception to section 31: the set is
    bounded by ``MAX_LINES_PER_ORDER`` at write time and a packing list is only
    meaningful whole.
    """

    lines: list[PackingOrderLineRead] = Field(default_factory=list)


class PackingOrderFilters(BaseModel):
    search: str | None = None
    status: PackingOrderStatus | None = None
    customer_id: uuid.UUID | None = None
    warehouse_id: uuid.UUID | None = None
    sales_order_id: uuid.UUID | None = None
    open_only: bool = False
    awaiting_dispatch: bool = False
    planned_from: date | None = None
    planned_to: date | None = None


# -----------------------------------------------------------------------------
# Cartons
# -----------------------------------------------------------------------------


class CartonContentIn(BaseModel):
    """One quantity of one item going into a carton.

    ``serial_ids`` is required for a serial-tracked item and refused for every
    other one: a serial identifies a single physical unit, so the count has to
    equal the quantity, and a non-serialised part has nothing to name. The
    quantity/count agreement is checked in the service, where the item's
    tracking mode is known.
    """

    model_config = ConfigDict(extra="forbid")

    line_number: LineNumber
    quantity: PositiveQuantity
    #: What this quantity of this item weighs. Zero is accepted - not every
    #: shipment is weighed per item - and simply produces a carton whose net
    #: weight is its tare.
    net_weight_kg: NonNegativeWeight = Decimal("0")
    batch_id: uuid.UUID | None = None
    serial_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SERIALS_PER_CONTENT)

    @model_validator(mode="after")
    def _serials_are_distinct(self) -> CartonContentIn:
        if len(set(self.serial_ids)) != len(self.serial_ids):
            raise ValueError("The same serial cannot be packed twice into one carton.")
        return self


class CartonCreate(BaseModel):
    """Open a new carton on the bench.

    ``carton_number`` and ``barcode`` are absent: both are assigned by the
    server. The number is the next one for this order and the barcode is derived
    from it, so a client cannot reserve either.
    """

    model_config = ConfigDict(extra="forbid")

    #: The item the box itself is. It is issued from stock when the carton is
    #: sealed - a carton is not free (section 22).
    packing_material_item_id: uuid.UUID
    packing_material_quantity: PositiveQuantity = Decimal("1")
    carton_type: CartonType = CartonType.BOX
    #: The empty box. Recorded now because once it is full it cannot be weighed
    #: on its own, and the gross weight is computed from it at sealing.
    tare_weight_kg: NonNegativeWeight = Decimal("0")

    length_cm: PositiveDimension | None = None
    width_cm: PositiveDimension | None = None
    height_cm: PositiveDimension | None = None

    #: Contents may be given now or added later while the carton is open.
    contents: list[CartonContentIn] = Field(
        default_factory=list, max_length=MAX_CONTENTS_PER_CARTON
    )

    @model_validator(mode="after")
    def _dimensions_are_complete(self) -> CartonCreate:
        given = [self.length_cm, self.width_cm, self.height_cm]
        if any(value is not None for value in given) and any(value is None for value in given):
            raise ValueError("Give all three dimensions or none: two of three computes no volume.")
        return self


class CartonContentsReplace(BaseModel):
    """Replace the whole content set of an open carton.

    Replacement rather than append: the packed quantities on the order's lines
    are maintained from these rows, and reconciling a partial edit against them
    is where an off-by-one becomes a customer receiving the wrong box.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    contents: list[CartonContentIn] = Field(
        default_factory=list, max_length=MAX_CONTENTS_PER_CARTON
    )


class SealCartonRequest(BaseModel):
    """Close a carton: freeze its contents and issue the box from stock."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    #: Business time of the sealing, when it differs from now - a night shift
    #: keyed in the following morning. It is the ``occurred_at`` of the packing
    #: material movement.
    occurred_at: datetime | None = None


class CartonContentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    carton_id: uuid.UUID
    packing_order_line_id: uuid.UUID
    item_id: uuid.UUID
    quantity: Decimal
    net_weight_kg: Decimal
    batch_id: uuid.UUID | None
    is_serial_tracked_item: bool
    serial_ids: list[uuid.UUID]


class CartonRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    packing_order_id: uuid.UUID
    carton_number: int
    carton_type: CartonType
    barcode: str
    length_cm: Decimal | None
    width_cm: Decimal | None
    height_cm: Decimal | None
    tare_weight_kg: Decimal
    net_weight_kg: Decimal
    gross_weight_kg: Decimal
    packing_material_item_id: uuid.UUID
    packing_material_quantity: Decimal
    material_transaction_id: uuid.UUID | None
    is_sealed: bool
    sealed_at: datetime | None


class CartonDetail(CartonRead):
    """A carton and everything in it.

    Contents are not paginated, by the same exception as the order's lines: the
    set is bounded by ``MAX_CONTENTS_PER_CARTON`` at write time, and half a
    carton's contents is not a useful answer to anybody.
    """

    contents: list[CartonContentRead] = Field(default_factory=list)


class CartonFilters(BaseModel):
    packing_order_id: uuid.UUID | None = None
    barcode: str | None = None
    is_sealed: bool | None = None


# -----------------------------------------------------------------------------
# Labels
# -----------------------------------------------------------------------------


class CartonLabelLine(BaseModel):
    """One printed line of a carton label's content block."""

    item_id: uuid.UUID
    item_code: str
    item_name: str
    quantity: Decimal
    batch_number: str | None = None
    serial_count: int = 0


class CartonLabel(BaseModel):
    """Everything a label printer needs, and nothing it does not.

    This is the *payload*, not the artwork. Rendering it - to a 100x150 mm
    thermal label, to a PDF, to ZPL - is the printer's job; generating pixels in
    the request cycle is what section 22 forbids. ``symbology`` is stated
    explicitly so a printer does not have to guess how to encode ``barcode``.
    """

    barcode: str
    symbology: str = "CODE128"
    packing_order_id: uuid.UUID
    packing_order_code: str
    carton_number: int
    total_cartons: int
    carton_type: CartonType
    customer_id: uuid.UUID
    customer_code: str
    customer_name: str
    sales_order_code: str | None = None
    gross_weight_kg: Decimal
    net_weight_kg: Decimal
    length_cm: Decimal | None = None
    width_cm: Decimal | None = None
    height_cm: Decimal | None = None
    is_sealed: bool
    sealed_at: datetime | None = None
    lines: list[CartonLabelLine] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Lifecycle payloads and results
# -----------------------------------------------------------------------------


class ReasonRequest(BaseModel):
    """The payload every state transition shares: why it happened."""

    model_config = ConfigDict(extra="forbid")

    reason: Reason


class CompleteRequest(BaseModel):
    """Finish packing.

    ``allow_short_pack`` is the deliberate escape hatch for a shipment that goes
    out incomplete. It is off by default so the ordinary case cannot silently
    ship less than the customer ordered, and it is recorded in the audit entry
    when it is used.
    """

    model_config = ConfigDict(extra="forbid")

    reason: Reason
    allow_short_pack: bool = False


class DispatchHandoverRequest(BaseModel):
    """Claim a packed order for a dispatch."""

    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID
    reason: Reason


class PackingCompletionResult(BaseModel):
    """What completion froze, so the caller does not have to re-read to find out."""

    order: PackingOrderDetail
    total_cartons: int
    total_gross_weight: Decimal
    total_net_weight: Decimal
    #: Lines whose packed quantity is short of what was asked for. Empty unless
    #: ``allow_short_pack`` was used.
    short_packed_line_numbers: list[int] = Field(default_factory=list)


class SealResult(BaseModel):
    """A sealed carton and the packing material it cost."""

    carton: CartonDetail
    #: The ``inventory_transactions`` row for the ``MATERIAL_ISSUE``. A caller
    #: reconciling a packing-material balance starts here.
    material_transaction_id: uuid.UUID
    packing_material_item_id: uuid.UUID
    packing_material_quantity: Decimal


class ReadyForDispatchRow(BaseModel):
    """A packed order no dispatch has claimed yet.

    This is the contract the ``dispatch`` module consumes. ``carton_count`` is
    counted from the carton rows rather than read from ``total_cartons`` so the
    feed cannot advertise a number the boxes disagree with.
    """

    model_config = ConfigDict(from_attributes=True)

    packing_order_id: uuid.UUID
    code: str
    customer_id: uuid.UUID
    warehouse_id: uuid.UUID
    sales_order_id: uuid.UUID | None
    packed_date: datetime | None
    carton_count: int
    total_gross_weight: Decimal
    total_net_weight: Decimal


class PackingMaterialRow(BaseModel):
    """An item offered as packing material on the carton form.

    A pick-list, not a rule: ``item_group`` is a reporting and default-behaviour
    classification (database.md section 3), so it decides what is *suggested*
    here and never what is *permitted* when a carton is sealed.
    """

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    code: str
    name: str
    base_uom_id: uuid.UUID
    available_quantity: Decimal


class PackingListJobAccepted(BaseModel):
    """Documented shape of the packing-list endpoint's ``data`` block."""

    job_id: str
    status: str
    status_url: str


__all__ = [
    "MAX_CARTONS_PER_ORDER",
    "MAX_SERIALS_PER_CONTENT",
    "READY_FOR_DISPATCH_MAX",
    "CartonContentIn",
    "CartonContentRead",
    "CartonContentsReplace",
    "CartonCreate",
    "CartonDetail",
    "CartonFilters",
    "CartonLabel",
    "CartonLabelLine",
    "CartonRead",
    "CompleteRequest",
    "DispatchHandoverRequest",
    "PackingCompletionResult",
    "PackingFromAssemblyCreate",
    "PackingListJobAccepted",
    "PackingMaterialRow",
    "PackingOrderCreate",
    "PackingOrderDetail",
    "PackingOrderFilters",
    "PackingOrderLineIn",
    "PackingOrderLineRead",
    "PackingOrderSummary",
    "PackingOrderUpdate",
    "ReadyForDispatchRow",
    "ReasonRequest",
    "SealCartonRequest",
    "SealResult",
]
