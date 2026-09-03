"""Packing business rules.

Master rules section 22, worked through the way a bench actually works: an order
is raised, boxes are opened against it, goods go in, each box is closed and
weighed, and the finished set is handed to dispatch. The rules that matter, and
why each lives here rather than in a caller:

1. **A carton is not free, and it is paid for once.** Sealing posts a
   ``MATERIAL_ISSUE`` for the box's own item through
   :func:`~src.modules.inventory.service.post_movements` and records the ledger
   row id on the carton. Posting at *creation* instead would mean every removed
   carton needed an offsetting movement; posting at *completion* would mean an
   order abandoned half-packed had eaten boxes the ledger never saw. Sealing is
   the moment a box is definitively used, so it is the moment it is issued - and
   a sealed carton can never be removed or re-sealed, which is what makes "once"
   true rather than merely intended.

2. **Sealing freezes the contents and computes the weight.** ``net_weight_kg``
   is the sum of the content rows, ``gross_weight_kg`` is that plus the empty
   box's ``tare_weight_kg``. After the seal no content row may be added, changed
   or removed: what is inside a closed box is a physical fact, not a field.

3. **Packed quantities are maintained from the carton contents, never typed.**
   Every content change recomputes the affected ``packing_order_lines`` rows
   inside the same transaction, under the header's row lock, and the database
   refuses ``packed_quantity > quantity_to_pack``. Over-packing is a mis-keyed
   quantity or goods nobody ordered; both need a person, so it is refused rather
   than clamped.

4. **Completing does not dispatch.** It freezes the totals and moves the order to
   ``PACKED``, which is what puts it on :func:`ready_for_dispatch`. No
   finished-goods movement is posted here at all - the goods leave when
   ``dispatch`` posts ``DISPATCH`` movements, and posting them in both places
   would decrement the same stock twice (database.md section 8).

5. **The packing list PDF is never rendered in the request.** The endpoint
   queues ``reports.generate_packing_list`` and returns ``202`` with a job id
   (section 22, section 35). A four-page PDF for a fifty-carton order is not
   something an HTTP worker should be holding a connection open for.

## Assumptions

Each of these is a rule the contract did not fix. The conservative option was
taken, named, and is changeable in one place.

* **The packing-material item is not gated on ``item_group``.** database.md
  section 3 is explicit that ``item_group`` is a reporting and default-behaviour
  classification and that no business rule may read it to decide whether an
  operation is permitted. So ``item_group == PACKING_MATERIAL`` decides what the
  carton form *offers* (``list_packing_materials``) and a carton sealed against
  anything else is logged as unusual - but it is not refused, because refusing
  it would be exactly the prohibited branch. The item must exist and be active;
  that is the check that is actually about permission.
* **A sealed carton cannot be unsealed.** There is no reversing movement in this
  module. A box packed wrongly is removed from the order by a stock adjustment
  and a new carton, which leaves a trail; an unseal would leave none.
* **Short-packing is allowed only explicitly.** ``CompleteRequest.allow_short_pack``
  defaults to false, so the ordinary case cannot silently ship less than was
  asked for, and using it is recorded on the audit entry.
* **Packing reserves nothing.** Section 22 gives packing no reservation
  contract, and taking a hard reservation here would double-count against the
  one the sales order already holds. The goods are protected by that
  reservation until dispatch consumes it.
* **``carton_number`` is never reused.** Removing carton 3 of 5 leaves a gap
  rather than renumbering 4 and 5, because their labels have already been
  printed and a renumber would make two different boxes claim one number.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    AppError,
    BusinessRuleError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.logging import get_logger
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import SerialStatus, TransactionType
from src.modules.inventory.service import MovementRequest, post_movements
from src.modules.packing.models import (
    BARCODE_LENGTH,
    CANCELLABLE_STATUSES,
    CARTON_EDITABLE_STATUSES,
    MAX_CARTONS_PER_ORDER,
    MAX_CONTENTS_PER_CARTON,
    Carton,
    CartonContent,
    CartonType,
    PackingOrder,
    PackingOrderLine,
    PackingOrderStatus,
)
from src.modules.packing.repository import (
    BatchLookupRepository,
    CartonContentRepository,
    CartonRepository,
    CustomerLookupRepository,
    ItemLookupRepository,
    PackingItem,
    PackingOrderLineRepository,
    PackingOrderRepository,
    SalesOrderLookupRepository,
    SerialLookupRepository,
    WarehouseLookupRepository,
)
from src.modules.packing.schemas import (
    READY_FOR_DISPATCH_MAX,
    CartonContentIn,
    CartonContentRead,
    CartonContentsReplace,
    CartonCreate,
    CartonDetail,
    CartonFilters,
    CartonLabel,
    CartonLabelLine,
    CompleteRequest,
    PackingCompletionResult,
    PackingMaterialRow,
    PackingOrderCreate,
    PackingOrderDetail,
    PackingOrderFilters,
    PackingOrderLineIn,
    PackingOrderLineRead,
    PackingOrderSummary,
    PackingOrderUpdate,
    ReadyForDispatchRow,
    SealCartonRequest,
    SealResult,
)
from src.modules.products.models import ItemGroup

log = get_logger(__name__)

AUDIT_PACKING_ORDER = "packing_order"
AUDIT_CARTON = "carton"

#: What the ledger rows this module posts point back at. A bare string, matching
#: ``inventory_transactions.reference_type`` - the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
MOVEMENT_REFERENCE_CARTON = "packing_carton"

#: The Celery task that renders a packing list. Dispatched by *name* so the API
#: process never imports the worker package (section 35).
PACKING_LIST_TASK_NAME = "reports.generate_packing_list"

#: Queue the packing-list job lands on. Report rendering is slow and must not sit
#: in front of a 200 ms notification.
PACKING_LIST_QUEUE = "reports"

#: Draft fields an explicit ``null`` in a PATCH body legitimately clears. Every
#: other column on the update payload backs a NOT NULL column, where a null is a
#: client mistake rather than an instruction - see :func:`update_order`.
CLEARABLE_UPDATE_FIELDS = frozenset({"notes"})

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
ORDER_AUDITED_FIELDS = [
    "code",
    "sales_order_id",
    "customer_id",
    "warehouse_id",
    "status",
    "planned_date",
    "packed_date",
    "packed_by_id",
    "total_cartons",
    "total_gross_weight",
    "total_net_weight",
    "dispatch_id",
    "handed_over_at",
    "notes",
]

CARTON_AUDITED_FIELDS = [
    "carton_number",
    "carton_type",
    "barcode",
    "length_cm",
    "width_cm",
    "height_cm",
    "tare_weight_kg",
    "net_weight_kg",
    "gross_weight_kg",
    "packing_material_item_id",
    "packing_material_quantity",
    "material_transaction_id",
    "is_sealed",
    "sealed_at",
]


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move packing_orders.status
# -----------------------------------------------------------------------------

PACKING_ORDER_LIFECYCLE: StateMachine[PackingOrderStatus] = StateMachine(
    entity="Packing order",
    initial=PackingOrderStatus.DRAFT,
    transitions=(
        Transition(
            PackingOrderStatus.DRAFT,
            PackingOrderStatus.IN_PROGRESS,
            permission=Permission.PACKING_MANAGE,
            label="Start packing",
        ),
        Transition(
            PackingOrderStatus.IN_PROGRESS,
            PackingOrderStatus.PACKED,
            permission=Permission.PACKING_MANAGE,
            label="Complete packing",
            # Freezes the totals a transporter is quoted against and publishes the
            # order to the dispatch feed.
            is_irreversible=True,
        ),
        Transition(
            PackingOrderStatus.PACKED,
            PackingOrderStatus.HANDED_OVER,
            # The dispatch module performs this one: it is the moment a dispatch
            # takes responsibility for the boxes.
            permission=Permission.DISPATCH_CREATE,
            label="Hand over to dispatch",
            is_irreversible=True,
        ),
        Transition(
            PackingOrderStatus.HANDED_OVER,
            PackingOrderStatus.CLOSED,
            permission=Permission.PACKING_MANAGE,
            label="Close",
        ),
        # Cancellation is reachable only before the order is packed. Past that,
        # boxes are sealed and packing material has been consumed, so the way out
        # is to hand over and close - the trail has to survive.
        Transition(
            PackingOrderStatus.DRAFT,
            PackingOrderStatus.CANCELLED,
            permission=Permission.PACKING_MANAGE,
            label="Cancel",
        ),
        Transition(
            PackingOrderStatus.IN_PROGRESS,
            PackingOrderStatus.CANCELLED,
            permission=Permission.PACKING_MANAGE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({PackingOrderStatus.CLOSED, PackingOrderStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Barcodes - pure, unit tested without a database
# -----------------------------------------------------------------------------

#: Base-36. Code 128 encodes the full ASCII range, but restricting the value to
#: upper-case alphanumerics keeps it printable in Code 39 too and removes every
#: character a human could mistype while reading a smudged label back.
BARCODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Width of the carton counter inside the barcode. Four digits covers
#: ``MAX_CARTONS_PER_ORDER`` with room to spare and keeps every barcode from one
#: order the same length, which matters to fixed-format label templates.
BARCODE_SEQUENCE_DIGITS = 4

#: Shape the database also enforces - see ``cartons.barcode_is_scannable``.
BARCODE_PATTERN = re.compile(rf"^[0-9A-Z-]{{8,{BARCODE_LENGTH}}}$")


def barcode_body(order_code: str) -> str:
    """Reduce a document number to the scannable part of a barcode.

    ``PK/2026-27/00042`` becomes ``PK20262700042``. Separators are dropped rather
    than encoded: a slash is legal in Code 128 but not in Code 39, and every
    character removed here is one fewer bar on a label that has to survive being
    read across a warehouse.
    """
    return "".join(ch for ch in order_code.upper() if ch.isalnum())


def barcode_check_character(payload: str) -> str:
    """Luhn mod-36 check character for ``payload``.

    A check character is not decoration. A packer reads a barcode back over the
    phone, or a scanner drops a bar, and the result is a value that still *looks*
    like a barcode. Luhn mod N catches every single-character substitution and
    almost every transposition of adjacent characters, which are the two errors
    that actually happen.
    """
    base = len(BARCODE_ALPHABET)
    total = 0
    factor = 2
    for character in reversed(payload):
        index = BARCODE_ALPHABET.find(character)
        if index < 0:
            raise ValueError(f"Character {character!r} is not in the barcode alphabet.")
        addend = factor * index
        # Fold the overflow back in, exactly as decimal Luhn does with its
        # two-digit products: without this, information above the base is lost
        # and the check stops detecting some substitutions.
        total += (addend // base) + (addend % base)
        factor = 1 if factor == 2 else 2
    return BARCODE_ALPHABET[(base - (total % base)) % base]


def build_barcode(*, order_code: str, carton_number: int) -> str:
    """The barcode value for one carton: ``<order body><sequence><check>``.

    Deterministic and derived from two things that are already unique - the
    order code is unique per company and the carton number is unique within the
    order - so the value is structurally unique before
    ``uq_cartons_company_id_barcode`` has to prove it. That matters because a
    random barcode would need a retry loop against that index, and a retry loop
    around a value printed on a physical label is a bad place to be.
    """
    if carton_number <= 0:
        raise ValueError("carton_number must be positive")

    body = barcode_body(order_code)
    if not body:
        raise ValueError("The order code contains no alphanumeric characters to encode.")

    payload = f"{body}{carton_number:0{BARCODE_SEQUENCE_DIGITS}d}"
    if len(payload) + 1 > BARCODE_LENGTH:
        # Truncating the body would let two orders share a barcode, which is the
        # one failure this value exists to prevent. Refusing is the safe answer.
        raise ValueError(
            f"A barcode for {order_code} carton {carton_number} would exceed "
            f"{BARCODE_LENGTH} characters."
        )
    return payload + barcode_check_character(payload)


def verify_barcode(value: str) -> bool:
    """Is this a well-formed barcode this system generated?

    Used to reject a mis-scanned value at the lookup endpoint before it becomes a
    query that finds nothing and looks like a missing carton.
    """
    candidate = value.strip().upper()
    if not BARCODE_PATTERN.match(candidate) or len(candidate) < 2:
        return False
    body, check = candidate[:-1], candidate[-1]
    try:
        return barcode_check_character(body) == check
    except ValueError:
        return False


# -----------------------------------------------------------------------------
# Weight and content arithmetic - pure, unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineFacts:
    """What the packed-quantity arithmetic needs from one order line."""

    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity_to_pack: Decimal
    packed_quantity: Decimal

    @property
    def remaining(self) -> Decimal:
        return to_quantity(self.quantity_to_pack - self.packed_quantity)


@dataclass(frozen=True, slots=True)
class PlannedContent:
    """One validated content row, ready to be written."""

    line_id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    quantity: Decimal
    net_weight_kg: Decimal
    batch_id: uuid.UUID | None
    is_serial_tracked_item: bool
    serial_ids: tuple[uuid.UUID, ...]


def net_weight_of(contents: Sequence[PlannedContent] | Sequence[CartonContent]) -> Decimal:
    """Net weight of a carton: the sum of what is in it.

    An empty carton weighs nothing net - its gross is its tare - which is why
    this returns zero rather than refusing. Whether an empty carton may be
    *sealed* is a separate rule, and it lives in :func:`seal_carton`.
    """
    return to_quantity(sum((Decimal(c.net_weight_kg) for c in contents), start=ZERO))


def gross_weight_of(*, net_weight_kg: Decimal, tare_weight_kg: Decimal) -> Decimal:
    """Gross weight of a sealed carton: its contents plus the empty box.

    Deliberately one line and deliberately here rather than inline: it is the
    number a transporter bills against, and it must be produced identically by
    the sealing path, the label and any later recalculation.
    """
    return to_quantity(Decimal(net_weight_kg) + Decimal(tare_weight_kg))


def plan_contents(
    requests: Sequence[CartonContentIn],
    *,
    lines: Mapping[int, LineFacts],
    items: Mapping[uuid.UUID, PackingItem],
) -> list[PlannedContent]:
    """Validate a requested content set against the order's lines and the item master.

    Five things have to be true of every row, and none of them is expressible as
    a database constraint alone:

    1. the line number is on this order - otherwise a carton could be filled with
       goods some other order is responsible for;
    2. the item is known and active;
    3. a batch-tracked item names a batch, because the ledger and the recall
       trail both need one;
    4. a serial-tracked item names exactly as many serials as it packed, and a
       non-serialised one names none;
    5. the same line does not appear twice in one carton, which would make the
       packed-quantity roll-up depend on iteration order.
    """
    planned: list[PlannedContent] = []
    seen: set[int] = set()

    for request in requests:
        if request.line_number in seen:
            raise ValidationError(
                f"Line {request.line_number} appears twice in this carton; "
                "combine the quantities into one content row.",
                details={"line_number": request.line_number},
            )
        seen.add(request.line_number)

        line = lines.get(request.line_number)
        if line is None:
            raise ValidationError(
                f"Line {request.line_number} is not on this packing order.",
                details={"line_number": request.line_number},
            )

        item = items.get(line.item_id)
        if item is None:
            raise NotFoundError("Item", line.item_id)
        if not item.is_active:
            raise BusinessRuleError(
                f"Item {item.code} is inactive and cannot be packed.",
                details={"item_id": str(item.id), "line_number": line.line_number},
            )

        quantity = to_quantity(request.quantity)

        if item.is_batch_tracked and request.batch_id is None:
            raise BusinessRuleError(
                f"Item {item.code} is batch tracked; name the batch in this carton.",
                code=ErrorCode.BATCH_REQUIRED,
                details={"line_number": line.line_number, "item_id": str(item.id)},
            )
        if not item.is_batch_tracked and request.batch_id is not None:
            raise BusinessRuleError(
                f"Item {item.code} is not batch tracked and cannot carry a batch.",
                details={"line_number": line.line_number, "item_id": str(item.id)},
            )

        if item.is_serial_tracked:
            if Decimal(len(request.serial_ids)) != quantity:
                raise BusinessRuleError(
                    f"Item {item.code} is serial tracked: name one serial per unit "
                    f"({quantity} expected, {len(request.serial_ids)} given).",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={
                        "line_number": line.line_number,
                        "quantity": str(quantity),
                        "serial_count": len(request.serial_ids),
                    },
                )
        elif request.serial_ids:
            raise BusinessRuleError(
                f"Item {item.code} is not serial tracked and cannot carry serial numbers.",
                details={"line_number": line.line_number, "item_id": str(item.id)},
            )

        planned.append(
            PlannedContent(
                line_id=line.id,
                line_number=line.line_number,
                item_id=line.item_id,
                quantity=quantity,
                net_weight_kg=to_quantity(request.net_weight_kg),
                batch_id=request.batch_id,
                is_serial_tracked_item=item.is_serial_tracked,
                serial_ids=tuple(request.serial_ids),
            )
        )

    return planned


def recompute_packed_quantities(
    *,
    lines: Sequence[LineFacts],
    released: Mapping[uuid.UUID, Decimal],
    added: Sequence[PlannedContent],
) -> dict[uuid.UUID, Decimal]:
    """New ``packed_quantity`` per line after a carton's contents change.

    ``released`` is what the carton being edited held before (zero for a new
    carton); ``added`` is what it will hold. Both are applied to the *current*
    stored quantities, which the caller has read under the header's row lock, so
    a concurrent carton on the same order cannot be lost.

    Over-packing raises rather than clamping: a quantity beyond what the order
    asks for is either mis-keyed or goods nobody ordered, and both need a person
    to look. A negative result is impossible from correct data, so it is treated
    as a bug and refused loudly rather than floored at zero.
    """
    per_line: dict[uuid.UUID, Decimal] = {}
    for content in added:
        per_line[content.line_id] = per_line.get(content.line_id, ZERO) + content.quantity

    updated: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        delta = per_line.get(line.id, ZERO) - released.get(line.id, ZERO)
        if delta == ZERO:
            continue

        value = to_quantity(Decimal(line.packed_quantity) + delta)
        if value < ZERO:
            raise BusinessRuleError(
                f"Line {line.line_number} would be packed to a negative quantity; "
                "the carton contents and the order line disagree.",
                details={"line_number": line.line_number, "packed_quantity": str(value)},
            )
        if value > to_quantity(Decimal(line.quantity_to_pack)):
            raise BusinessRuleError(
                f"Line {line.line_number} would be over-packed: "
                f"{value} against {line.quantity_to_pack} requested.",
                details={
                    "line_number": line.line_number,
                    "requested": str(line.quantity_to_pack),
                    "would_be_packed": str(value),
                },
            )
        updated[line.id] = value

    return updated


def build_material_movement(
    carton: Carton,
    *,
    warehouse_id: uuid.UUID,
    reason: str,
    occurred_at: datetime | None,
) -> MovementRequest:
    """The single ledger row sealing one carton writes.

    A ``MATERIAL_ISSUE`` against the box's own item: the carton left the packing
    -material shelf and is now around somebody's goods. ``reference_id`` is the
    carton and ``reference_code`` its barcode, so a packing-material variance can
    be traced to the exact box that consumed it.
    """
    return MovementRequest(
        item_id=carton.packing_material_item_id,
        warehouse_id=warehouse_id,
        quantity=to_quantity(Decimal(carton.packing_material_quantity)),
        transaction_type=TransactionType.MATERIAL_ISSUE,
        reference_type=MOVEMENT_REFERENCE_CARTON,
        reference_id=carton.id,
        reference_code=carton.barcode,
        reason=reason,
        occurred_at=occurred_at,
    )


# -----------------------------------------------------------------------------
# Shared helpers
# -----------------------------------------------------------------------------


def _line_facts(lines: Sequence[PackingOrderLine]) -> list[LineFacts]:
    return [
        LineFacts(
            id=line.id,
            line_number=line.line_number,
            item_id=line.item_id,
            quantity_to_pack=Decimal(line.quantity_to_pack),
            packed_quantity=Decimal(line.packed_quantity),
        )
        for line in lines
    ]


def order_detail(
    order: PackingOrder, lines: Sequence[PackingOrderLine]
) -> PackingOrderDetail:
    detail = PackingOrderDetail.model_validate(order)
    detail.lines = [PackingOrderLineRead.model_validate(line) for line in lines]
    return detail


def carton_detail(carton: Carton, contents: Sequence[CartonContent]) -> CartonDetail:
    detail = CartonDetail.model_validate(carton)
    detail.contents = [CartonContentRead.model_validate(row) for row in contents]
    return detail


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    target: PackingOrderStatus,
    *,
    reason: str,
    repo: PackingOrderRepository,
    action: AuditAction = AuditAction.STATUS_CHANGE,
) -> PackingOrder:
    """Move an order's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``packing_orders.status`` and exactly one place that writes the
    matching audit row (master rules section 73).
    """
    current = PackingOrderStatus(order.status)
    PACKING_ORDER_LIFECYCLE.assert_transition(current, target)
    order.status = target
    repo.touch(order)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_PACKING_ORDER,
        entity_id=order.id,
        entity_code=order.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return order


async def _lock_order(repo: PackingOrderRepository, order_id: uuid.UUID) -> PackingOrder:
    """Take the header's row lock.

    Must be called *inside* a :func:`~src.core.database.transaction` block. A
    ``SELECT ... FOR UPDATE`` issued outside one is held only by the implicit
    transaction SQLAlchemy opens for the statement, which nothing in the request
    lifecycle then commits - so the lock would be released at an unpredictable
    point and the read-modify-write it exists to protect would not be protected
    at all (database.md section 4.4).
    """
    order = await repo.lock_for_update(order_id)
    if order is None:
        raise NotFoundError("Packing order", order_id)
    return order


def _assert_cartons_editable(order: PackingOrder) -> None:
    if PackingOrderStatus(order.status) not in CARTON_EDITABLE_STATUSES:
        raise BusinessRuleError(
            f"{order.code} is {order.status}; cartons may only be changed while packing "
            "is in progress.",
            details={"order_id": str(order.id), "status": str(order.status)},
        )


async def _validate_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    lines: Sequence[PackingOrderLineIn],
    sales_order_id: uuid.UUID | None,
) -> dict[uuid.UUID, PackingItem]:
    """Check a whole line set in three batched statements, not three per line.

    Items must exist and be active. A named sales order line must belong to the
    sales order on the header and be for the same item - a link to either the
    wrong order or the wrong item would corrupt the fulfilment arithmetic the
    sales module does against it, in a way nothing downstream could detect.
    """
    item_ids = [line.item_id for line in lines]
    items = await ItemLookupRepository(session, tenant).load(item_ids)

    for line in lines:
        item = items.get(line.item_id)
        if item is None:
            raise NotFoundError("Item", line.item_id)
        if not item.is_active:
            raise BusinessRuleError(
                f"Item {item.code} is inactive and cannot be packed.",
                details={"item_id": str(item.id)},
            )

    sales_line_ids = [
        line.sales_order_line_id for line in lines if line.sales_order_line_id is not None
    ]
    if not sales_line_ids:
        return items

    if sales_order_id is None:
        raise ValidationError(
            "A packing line cannot reference a sales order line when the order names no "
            "sales order."
        )

    known = await SalesOrderLookupRepository(session, tenant).load_lines(sales_line_ids)
    for line in lines:
        if line.sales_order_line_id is None:
            continue
        found = known.get(line.sales_order_line_id)
        if found is None:
            raise NotFoundError("Sales order line", line.sales_order_line_id)
        owner_order_id, sales_item_id = found
        if owner_order_id != sales_order_id:
            raise BusinessRuleError(
                "That sales order line belongs to a different sales order.",
                details={"sales_order_line_id": str(line.sales_order_line_id)},
            )
        if sales_item_id != line.item_id:
            raise BusinessRuleError(
                "That sales order line is for a different item.",
                details={
                    "sales_order_line_id": str(line.sales_order_line_id),
                    "item_id": str(line.item_id),
                },
            )

    return items


async def _persist_lines(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    payload_lines: Sequence[PackingOrderLineIn],
) -> list[PackingOrderLine]:
    repo = PackingOrderLineRepository(session, tenant)
    rows = [
        repo.add(
            PackingOrderLine(
                packing_order_id=order.id,
                line_number=number,
                sales_order_line_id=line.sales_order_line_id,
                item_id=line.item_id,
                quantity_to_pack=to_quantity(line.quantity_to_pack),
                packed_quantity=ZERO,
                uom_id=line.uom_id,
            )
        )
        for number, line in enumerate(payload_lines, start=1)
    ]
    await session.flush()
    return rows


async def _validate_serials(
    session: AsyncSession,
    tenant: TenantContext,
    planned: Sequence[PlannedContent],
    *,
    warehouse_id: uuid.UUID,
    carton_id: uuid.UUID | None,
) -> None:
    """Check every named serial in one batch, not one query per content row.

    Four things have to be true before a serialised unit goes in a box: it exists
    in this tenant, it belongs to the line's item, it is physically at the
    warehouse this order packs from, and no other carton already holds it. The
    last one is a containment query rather than a constraint because "appears in
    at most one array across the table" is not expressible as a CHECK.
    """
    wanted = [serial_id for content in planned for serial_id in content.serial_ids]
    if not wanted:
        return

    facts = await SerialLookupRepository(session, tenant).load(wanted)
    already = await CartonContentRepository(session, tenant).already_packed(
        wanted, exclude_carton_id=carton_id
    )

    for content in planned:
        for serial_id in content.serial_ids:
            serial = facts.get(serial_id)
            if serial is None:
                raise NotFoundError("Stock serial", serial_id)
            if serial.item_id != content.item_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} belongs to a different item than "
                    f"line {content.line_number}.",
                    details={"serial_id": str(serial_id), "line_number": content.line_number},
                )
            if serial.warehouse_id != warehouse_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} is not held at the warehouse this order "
                    "packs from.",
                    details={
                        "serial_id": str(serial_id),
                        "serial_warehouse_id": str(serial.warehouse_id),
                        "order_warehouse_id": str(warehouse_id),
                    },
                )
            if serial.status is not SerialStatus.IN_STOCK:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} is {serial.status} and is not in stock.",
                    details={"serial_id": str(serial_id), "status": str(serial.status)},
                )
            packed_in = already.get(serial_id)
            if packed_in is not None:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} is already in carton {packed_in}.",
                    code=ErrorCode.CONFLICT,
                    details={"serial_id": str(serial_id), "barcode": packed_in},
                )


async def _validate_batches(
    session: AsyncSession, tenant: TenantContext, planned: Sequence[PlannedContent]
) -> None:
    """A named batch must exist and belong to the item it is packed against."""
    wanted = [c.batch_id for c in planned if c.batch_id is not None]
    if not wanted:
        return

    facts = await BatchLookupRepository(session, tenant).load(wanted)
    for content in planned:
        if content.batch_id is None:
            continue
        batch = facts.get(content.batch_id)
        if batch is None:
            raise NotFoundError("Stock batch", content.batch_id)
        if batch.item_id != content.item_id:
            raise BusinessRuleError(
                f"Batch {batch.batch_number} belongs to a different item than "
                f"line {content.line_number}.",
                details={"batch_id": str(content.batch_id), "line_number": content.line_number},
            )


async def _apply_packed_quantities(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    *,
    released: Mapping[uuid.UUID, Decimal],
    added: Sequence[PlannedContent],
) -> None:
    """Roll the carton's contents up onto the order's lines.

    The lines are locked in primary-key order before anything is read from them:
    the new value depends on the old one, and a value read before the lock is
    worthless (database.md section 4.4).
    """
    repo = PackingOrderLineRepository(session, tenant)
    rows = await repo.lock_for_order(order.id)
    updated = recompute_packed_quantities(
        lines=_line_facts(rows), released=released, added=added
    )
    if not updated:
        return

    by_id = {row.id: row for row in rows}
    for line_id, value in updated.items():
        line = by_id[line_id]
        line.packed_quantity = value
        repo.touch(line)
    await session.flush()


def _released_quantities(contents: Sequence[CartonContent]) -> dict[uuid.UUID, Decimal]:
    released: dict[uuid.UUID, Decimal] = {}
    for row in contents:
        released[row.packing_order_line_id] = released.get(
            row.packing_order_line_id, ZERO
        ) + Decimal(row.quantity)
    return released


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_orders(
    session: AsyncSession,
    tenant: TenantContext,
    filters: PackingOrderFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[PackingOrderSummary], OffsetPagination]:
    """Paginated order list.

    Headers only. Loading each order's lines here would be a query per row, and
    a list view has no use for them - the detail read is where they belong.
    """
    rows, pagination = await PackingOrderRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    return [PackingOrderSummary.model_validate(row) for row in rows], pagination


async def get_order_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> PackingOrderDetail:
    repo = PackingOrderRepository(session, tenant)
    order = await repo.get_or_404(order_id)
    lines = await PackingOrderLineRepository(session, tenant).list_for_order(order_id)
    return order_detail(order, lines)


async def list_cartons(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[CartonDetail], OffsetPagination]:
    """Cartons of one order, with their contents, in two statements.

    The header is fetched first so a missing order 404s rather than returning an
    empty carton set that looks like an unpacked order. The contents are loaded
    for the whole page at once - a content read per carton is the N+1 section 32
    forbids.
    """
    await PackingOrderRepository(session, tenant).get_or_404(order_id)

    filters = CartonFilters(packing_order_id=order_id)
    rows, pagination = await CartonRepository(session, tenant).list_page_filtered(
        filters, params, sort
    )
    contents = await CartonContentRepository(session, tenant).list_for_cartons(
        [row.id for row in rows]
    )
    return [carton_detail(row, contents.get(row.id, [])) for row in rows], pagination


async def get_carton_detail(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, carton_id: uuid.UUID
) -> CartonDetail:
    carton = await CartonRepository(session, tenant).get_in_order(order_id, carton_id)
    if carton is None:
        raise NotFoundError("Carton", carton_id)
    contents = await CartonContentRepository(session, tenant).list_for_carton(carton.id)
    return carton_detail(carton, contents)


async def list_packing_materials(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID,
    params: PageParams,
    search: str | None = None,
) -> tuple[list[PackingMaterialRow], OffsetPagination]:
    """The carton form's pick-list: packing-material items and what is on the shelf.

    See the module's Assumptions for why ``item_group`` selects this list but
    does not gate the seal.
    """
    return await ItemLookupRepository(session, tenant).packing_materials(
        warehouse_id=warehouse_id, params=params, search=search
    )


async def find_cartons_holding_serial(
    session: AsyncSession, tenant: TenantContext, serial_id: uuid.UUID
) -> list[CartonDetail]:
    """"Which carton contains this serial?" - the question this module exists for.

    Answered through the GIN index on ``carton_contents.serial_ids``. A correct
    data set returns at most one carton; the return type is a list because the
    containment guard is application-level, and an answer of two is a data
    incident somebody must be able to see rather than one this function hides.
    """
    contents = await CartonContentRepository(session, tenant).find_by_serial(serial_id)
    if not contents:
        return []

    carton_repo = CartonRepository(session, tenant)
    cartons = await carton_repo.get_many([row.carton_id for row in contents])
    grouped: dict[uuid.UUID, list[CartonContent]] = {}
    for row in contents:
        grouped.setdefault(row.carton_id, []).append(row)
    return [carton_detail(carton, grouped.get(carton.id, [])) for carton in cartons]


async def ready_for_dispatch(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID | None = None,
    limit: int = READY_FOR_DISPATCH_MAX,
) -> list[ReadyForDispatchRow]:
    """Packed orders waiting for a truck - the dispatch handover.

    This is the contract the ``dispatch`` module consumes. An order appears here
    when it is PACKED and no dispatch has claimed it; completing a packing order
    does not dispatch it, it only puts it on this list (section 22).

    ``limit`` is capped rather than trusted: this returns a plain list, so the
    bound has to live here (section 31).
    """
    bounded = max(1, min(int(limit), READY_FOR_DISPATCH_MAX))
    return await PackingOrderRepository(session, tenant).ready_for_dispatch(
        warehouse_id=warehouse_id, limit=bounded
    )


async def list_ready_for_dispatch(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    warehouse_id: uuid.UUID | None,
    params: PageParams,
) -> tuple[list[ReadyForDispatchRow], OffsetPagination]:
    """Paginated form of the same feed, for the HTTP surface."""
    return await PackingOrderRepository(session, tenant).ready_for_dispatch_page(
        warehouse_id=warehouse_id, params=params
    )


# -----------------------------------------------------------------------------
# Create / update / delete
# -----------------------------------------------------------------------------


async def create_order(
    session: AsyncSession, tenant: TenantContext, payload: PackingOrderCreate
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Raise a DRAFT packing order with the lines it has to fill.

    Nothing is boxed and nothing is consumed: a draft is a plan for a bench, and
    opening cartons against it before anybody has started would consume packing
    material for work that may never happen.
    """
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        customer = await CustomerLookupRepository(session, tenant).get_facts(payload.customer_id)
        if customer is None:
            raise NotFoundError("Customer", payload.customer_id)
        if not customer.is_active:
            raise BusinessRuleError(
                f"Customer {customer.code} is inactive.",
                details={"customer_id": str(customer.id)},
            )
        if customer.is_blocked:
            raise BusinessRuleError(
                f"Customer {customer.code} is blocked; goods may not be packed for them.",
                details={"customer_id": str(customer.id)},
            )

        warehouses = await WarehouseLookupRepository(session, tenant).load(
            [payload.warehouse_id]
        )
        found = warehouses.get(payload.warehouse_id)
        if found is None:
            raise NotFoundError("Warehouse", payload.warehouse_id)
        if not found[1]:
            raise BusinessRuleError(
                f"Warehouse {found[0]} is inactive and cannot be packed from.",
                details={"warehouse_id": str(payload.warehouse_id)},
            )

        if payload.sales_order_id is not None:
            header = await SalesOrderLookupRepository(session, tenant).get_header(
                payload.sales_order_id
            )
            if header is None:
                raise NotFoundError("Sales order", payload.sales_order_id)
            if header[1] != payload.customer_id:
                # Packing one customer's order into another's shipment is how
                # goods reach the wrong address, and no later check catches it.
                raise BusinessRuleError(
                    f"Sales order {header[0]} belongs to a different customer.",
                    details={"sales_order_id": str(payload.sales_order_id)},
                )

        await _validate_lines(
            session,
            tenant,
            lines=payload.lines,
            sales_order_id=payload.sales_order_id,
        )

        code = await next_document_number(session, tenant.company_id, DocTypes.PACKING_ORDER)
        order = repo.add(
            PackingOrder(
                code=code,
                sales_order_id=payload.sales_order_id,
                customer_id=payload.customer_id,
                warehouse_id=payload.warehouse_id,
                status=PackingOrderStatus.DRAFT,
                planned_date=payload.planned_date,
                notes=payload.notes,
            )
        )
        await session.flush()

        lines = await _persist_lines(session, tenant, order, payload.lines)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"Created {order.code}: pack {len(lines)} line(s) for {customer.code}"
            ),
            user_id=tenant.user_id,
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "customer_code": customer.code,
                "line_count": len(lines),
            },
        )

    return order, lines


async def update_order(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: PackingOrderUpdate,
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Amend a draft.

    Supplying ``lines`` replaces the whole set. That is safe only in DRAFT, where
    no carton content references a line; from IN_PROGRESS a replaced line would
    orphan a physical box, which the RESTRICT foreign key on
    ``carton_contents.packing_order_line_id`` refuses anyway.
    """
    repo = PackingOrderRepository(session, tenant)
    line_repo = PackingOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if PackingOrderStatus(order.status) is not PackingOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only a draft may be edited.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        before = snapshot(order, ORDER_AUDITED_FIELDS)
        # exclude_unset so an absent field is left alone; a field explicitly set
        # to null is only honoured where null is a meaningful value.
        changes = payload.model_dump(exclude_unset=True)
        new_lines = changes.pop("lines", None)

        if "warehouse_id" in changes and changes["warehouse_id"] is not None:
            warehouses = await WarehouseLookupRepository(session, tenant).load(
                [changes["warehouse_id"]]
            )
            found = warehouses.get(changes["warehouse_id"])
            if found is None:
                raise NotFoundError("Warehouse", changes["warehouse_id"])
            if not found[1]:
                raise BusinessRuleError(
                    f"Warehouse {found[0]} is inactive and cannot be packed from.",
                    details={"warehouse_id": str(changes["warehouse_id"])},
                )

        for field, value in changes.items():
            if value is None and field not in CLEARABLE_UPDATE_FIELDS:
                continue
            setattr(order, field, value)

        if new_lines is not None:
            parsed = [PackingOrderLineIn.model_validate(line) for line in new_lines]
            await _validate_lines(
                session, tenant, lines=parsed, sales_order_id=order.sales_order_id
            )
            await line_repo.delete_for_order(order.id)
            await _persist_lines(session, tenant, order, parsed)

        repo.touch(order)
        await session.flush()

        lines = await line_repo.list_for_order(order.id)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Updated {order.code}",
            user_id=tenant.user_id,
            old_values={**before, "line_count": None if new_lines is None else len(lines)},
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "line_count": None if new_lines is None else len(lines),
            },
        )

    return order, lines


async def delete_draft_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    """Hard-delete a draft.

    A draft has no cartons, no ledger rows and no obligation to anybody, so
    section 28's prohibition on hiding history does not apply - there is no
    history yet. Anything past DRAFT is cancelled or closed instead, and the
    audit entry below is what remembers the deletion happened.
    """
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if PackingOrderStatus(order.status) is not PackingOrderStatus.DRAFT:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; only a draft may be deleted. "
                "Cancel it instead.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )

        captured = snapshot(order, ORDER_AUDITED_FIELDS)
        code = order.code
        await PackingOrderLineRepository(session, tenant).delete_for_order(order.id)
        await repo.hard_delete(order)

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order_id,
            entity_code=code,
            summary=f"Deleted draft packing order {code}",
            user_id=tenant.user_id,
            old_values=captured,
            context={"reason": reason},
        )


# -----------------------------------------------------------------------------
# Cartons
# -----------------------------------------------------------------------------


async def add_carton(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CartonCreate,
) -> CartonDetail:
    """Open a new carton on the bench, optionally with its first contents.

    The header is locked first: the carton number is ``MAX + 1`` within the
    order, and the packed quantities are read-modify-write over the lines, so
    both need the two packers on one order to be serialised behind one row.
    Nothing is issued from stock here - the box is paid for when it is sealed.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        _assert_cartons_editable(order)

        material = await _resolve_packing_material(
            session, tenant, payload.packing_material_item_id, order=order
        )

        number = await carton_repo.next_carton_number(order.id)
        if number > MAX_CARTONS_PER_ORDER:
            # The bound is on the *sequence*, not on the live carton count: a
            # removed carton's number is never reused, so an order that has
            # churned through the range is as full as one holding every box.
            raise BusinessRuleError(
                f"{order.code} has used all {MAX_CARTONS_PER_ORDER} carton numbers; split "
                "the shipment across another packing order.",
                details={
                    "order_id": str(order.id),
                    "limit": MAX_CARTONS_PER_ORDER,
                    "next_carton_number": number,
                },
            )

        carton = carton_repo.add(
            Carton(
                packing_order_id=order.id,
                carton_number=number,
                carton_type=payload.carton_type,
                barcode=build_barcode(order_code=order.code, carton_number=number),
                length_cm=payload.length_cm,
                width_cm=payload.width_cm,
                height_cm=payload.height_cm,
                tare_weight_kg=to_quantity(payload.tare_weight_kg),
                net_weight_kg=ZERO,
                gross_weight_kg=to_quantity(payload.tare_weight_kg),
                packing_material_item_id=material.id,
                packing_material_quantity=to_quantity(payload.packing_material_quantity),
                is_sealed=False,
            )
        )
        await session.flush()

        contents: list[CartonContent] = []
        if payload.contents:
            contents = await _write_contents(
                session,
                tenant,
                order=order,
                carton=carton,
                requests=payload.contents,
                released={},
            )

        repo.touch(order)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_CARTON,
            entity_id=carton.id,
            entity_code=carton.barcode,
            summary=(
                f"Opened carton {carton.carton_number} ({carton.barcode}) on {order.code}"
            ),
            user_id=tenant.user_id,
            new_values={
                **snapshot(carton, CARTON_AUDITED_FIELDS),
                "packing_order_code": order.code,
                "content_count": len(contents),
            },
        )

    return carton_detail(carton, contents)


async def replace_carton_contents(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    payload: CartonContentsReplace,
) -> CartonDetail:
    """Set what is in an open carton, replacing whatever was there.

    The previous contents are given back to the order's lines and the new set is
    taken from them, both inside one transaction under the header's lock, so the
    packed quantities can never reflect half an edit.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    content_repo = CartonContentRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        _assert_cartons_editable(order)

        carton = await carton_repo.get_in_order(order.id, carton_id)
        if carton is None:
            raise NotFoundError("Carton", carton_id)
        if carton.is_sealed:
            raise BusinessRuleError(
                f"Carton {carton.barcode} is sealed; what is inside a closed box cannot "
                "be edited.",
                details={"carton_id": str(carton.id), "barcode": carton.barcode},
            )

        existing = await content_repo.list_for_carton(carton.id)
        released = _released_quantities(existing)
        before = {
            "content_count": len(existing),
            "net_weight_kg": Decimal(carton.net_weight_kg),
        }
        await content_repo.delete_for_carton(carton.id)

        contents = await _write_contents(
            session,
            tenant,
            order=order,
            carton=carton,
            requests=payload.contents,
            released=released,
        )

        repo.touch(order)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_CARTON,
            entity_id=carton.id,
            entity_code=carton.barcode,
            summary=f"Set the contents of carton {carton.barcode} on {order.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values={
                "content_count": len(contents),
                "net_weight_kg": Decimal(carton.net_weight_kg),
            },
            context={"reason": payload.reason},
        )

    return carton_detail(carton, contents)


async def _write_contents(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    order: PackingOrder,
    carton: Carton,
    requests: Sequence[CartonContentIn],
    released: Mapping[uuid.UUID, Decimal],
) -> list[CartonContent]:
    """Validate, persist and roll up one carton's content set.

    Shared by carton creation and content replacement so the validation, the
    packed-quantity roll-up and the carton's own net weight cannot drift apart
    between the two entry points.
    """
    if len(requests) > MAX_CONTENTS_PER_CARTON:
        raise BusinessRuleError(
            f"A carton may hold at most {MAX_CONTENTS_PER_CARTON} distinct content rows.",
            details={"limit": MAX_CONTENTS_PER_CARTON, "given": len(requests)},
        )

    line_repo = PackingOrderLineRepository(session, tenant)
    lines = await line_repo.list_for_order(order.id)
    by_number = {facts.line_number: facts for facts in _line_facts(lines)}

    items = await ItemLookupRepository(session, tenant).load(
        [line.item_id for line in lines]
    )
    planned = plan_contents(requests, lines=by_number, items=items)

    await _validate_batches(session, tenant, planned)
    await _validate_serials(
        session, tenant, planned, warehouse_id=order.warehouse_id, carton_id=carton.id
    )

    content_repo = CartonContentRepository(session, tenant)
    rows = [
        content_repo.add(
            CartonContent(
                carton_id=carton.id,
                packing_order_line_id=content.line_id,
                item_id=content.item_id,
                quantity=content.quantity,
                net_weight_kg=content.net_weight_kg,
                batch_id=content.batch_id,
                is_serial_tracked_item=content.is_serial_tracked_item,
                serial_ids=list(content.serial_ids),
            )
        )
        for content in planned
    ]

    carton.net_weight_kg = net_weight_of(planned)
    # The gross is only *frozen* at sealing, but keeping it consistent as the
    # carton fills means the ``gross >= net`` CHECK holds at every flush rather
    # than only at the end.
    carton.gross_weight_kg = gross_weight_of(
        net_weight_kg=carton.net_weight_kg, tare_weight_kg=carton.tare_weight_kg
    )
    CartonRepository(session, tenant).touch(carton)
    await session.flush()

    await _apply_packed_quantities(
        session, tenant, order, released=released, added=planned
    )
    return rows


async def _resolve_packing_material(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    *,
    order: PackingOrder,
) -> PackingItem:
    """The item this carton is made of.

    Existence and activity are checked because they are genuinely about whether
    the operation may proceed. ``item_group`` is *not* checked - see the module's
    Assumptions - but a carton sealed against something that is not classified as
    packing material is logged, because it is almost always a mis-picked item and
    somebody should be able to find it later.
    """
    items = await ItemLookupRepository(session, tenant).load([item_id])
    item = items.get(item_id)
    if item is None:
        raise NotFoundError("Item", item_id)
    if not item.is_active:
        raise BusinessRuleError(
            f"Item {item.code} is inactive and cannot be used as packing material.",
            details={"item_id": str(item_id)},
        )
    if item.is_serial_tracked:
        # The seal posts one movement for the whole carton quantity, and the
        # inventory engine demands a serial per unit for a serialised item. There
        # is no contract in this module for choosing which box serial was used.
        raise BusinessRuleError(
            f"Item {item.code} is serial tracked and cannot be used as packing material.",
            details={"item_id": str(item_id)},
        )
    if item.is_batch_tracked:
        raise BusinessRuleError(
            f"Item {item.code} is batch tracked and cannot be used as packing material: "
            "the seal has no batch to post against.",
            code=ErrorCode.BATCH_REQUIRED,
            details={"item_id": str(item_id)},
        )
    if item.item_group is not ItemGroup.PACKING_MATERIAL:
        log.warning(
            "packing.unexpected_packing_material",
            packing_order_code=order.code,
            item_code=item.code,
            item_group=str(item.item_group),
        )
    return item


async def seal_carton(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    payload: SealCartonRequest,
) -> SealResult:
    """Close a carton: freeze its contents, compute its gross weight, pay for the box.

    The ordering inside the transaction is deliberate. The weights are written
    first, then the ``MATERIAL_ISSUE`` is posted, and only then are ``is_sealed``
    and ``material_transaction_id`` set - because ``post_movements`` flushes, and
    a carton flushed as sealed with no ledger row would trip the
    ``sealed_carton_posted_its_material`` CHECK before the movement it is waiting
    for had been written.

    A second seal is refused rather than ignored: making it idempotent would mean
    deciding whether the caller wanted a second box issued, and the honest answer
    is that nobody knows.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    content_repo = CartonContentRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        _assert_cartons_editable(order)

        carton = await carton_repo.get_in_order(order.id, carton_id)
        if carton is None:
            raise NotFoundError("Carton", carton_id)
        if carton.is_sealed:
            raise BusinessRuleError(
                f"Carton {carton.barcode} is already sealed.",
                code=ErrorCode.CONFLICT,
                details={"carton_id": str(carton.id), "barcode": carton.barcode},
            )

        contents = await content_repo.list_for_carton(carton.id)
        if not contents:
            raise BusinessRuleError(
                f"Carton {carton.barcode} is empty; sealing it would consume a box for "
                "nothing.",
                details={"carton_id": str(carton.id), "barcode": carton.barcode},
            )

        before = snapshot(carton, CARTON_AUDITED_FIELDS)
        carton.net_weight_kg = net_weight_of(contents)
        carton.gross_weight_kg = gross_weight_of(
            net_weight_kg=carton.net_weight_kg, tare_weight_kg=carton.tare_weight_kg
        )
        carton_repo.touch(carton)
        await session.flush()

        posted = await post_movements(
            session,
            tenant,
            [
                build_material_movement(
                    carton,
                    warehouse_id=order.warehouse_id,
                    reason=payload.reason,
                    occurred_at=payload.occurred_at,
                )
            ],
        )

        carton.material_transaction_id = posted[0].id
        carton.is_sealed = True
        carton.sealed_at = utcnow()
        carton_repo.touch(carton)
        repo.touch(order)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.STATUS_CHANGE,
            entity_type=AUDIT_CARTON,
            entity_id=carton.id,
            entity_code=carton.barcode,
            summary=(
                f"Sealed carton {carton.carton_number} ({carton.barcode}) on {order.code} "
                f"at {carton.gross_weight_kg} kg gross"
            ),
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(carton, CARTON_AUDITED_FIELDS),
            context={"reason": payload.reason, "packing_order_code": order.code},
        )

    return SealResult(
        carton=carton_detail(carton, contents),
        material_transaction_id=posted[0].id,
        packing_material_item_id=carton.packing_material_item_id,
        packing_material_quantity=Decimal(carton.packing_material_quantity),
    )


async def remove_carton(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    *,
    reason: str,
) -> None:
    """Take an unsealed carton off the order and give its contents back to the lines.

    Only an unsealed carton. A sealed one has consumed packing material and has a
    printed label in the world; removing it would need a reversing movement this
    module deliberately does not have - see the Assumptions. Its number is not
    reused either, for the same reason.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    content_repo = CartonContentRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        _assert_cartons_editable(order)

        carton = await carton_repo.get_in_order(order.id, carton_id)
        if carton is None:
            raise NotFoundError("Carton", carton_id)
        if carton.is_sealed:
            raise BusinessRuleError(
                f"Carton {carton.barcode} is sealed and has already consumed its packing "
                "material; it cannot be removed.",
                details={"carton_id": str(carton.id), "barcode": carton.barcode},
            )

        contents = await content_repo.list_for_carton(carton.id)
        released = _released_quantities(contents)
        captured = snapshot(carton, CARTON_AUDITED_FIELDS)
        barcode = carton.barcode

        await content_repo.delete_for_carton(carton.id)
        await carton_repo.hard_delete(carton)
        await _apply_packed_quantities(session, tenant, order, released=released, added=[])

        repo.touch(order)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_CARTON,
            entity_id=carton_id,
            entity_code=barcode,
            summary=f"Removed unsealed carton {barcode} from {order.code}",
            user_id=tenant.user_id,
            old_values={**captured, "content_count": len(contents)},
            context={"reason": reason, "packing_order_code": order.code},
        )


async def carton_label(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, carton_id: uuid.UUID
) -> CartonLabel:
    """The printable label payload for one carton.

    A payload, not artwork: barcode value, symbology, addressee, weights and the
    content block. Turning it into a 100x150 mm thermal label or a PDF is the
    printer's job - generating pixels in the request cycle is what section 22
    forbids.
    """
    carton = await CartonRepository(session, tenant).get_in_order(order_id, carton_id)
    if carton is None:
        raise NotFoundError("Carton", carton_id)

    order = await PackingOrderRepository(session, tenant).get_or_404(order_id)
    contents = await CartonContentRepository(session, tenant).list_for_carton(carton.id)

    customer = await CustomerLookupRepository(session, tenant).get_facts(order.customer_id)
    if customer is None:
        raise NotFoundError("Customer", order.customer_id)

    sales_order_code: str | None = None
    if order.sales_order_id is not None:
        header = await SalesOrderLookupRepository(session, tenant).get_header(
            order.sales_order_id
        )
        sales_order_code = header[0] if header is not None else None

    items = await ItemLookupRepository(session, tenant).load([row.item_id for row in contents])
    batches = await BatchLookupRepository(session, tenant).load(
        [row.batch_id for row in contents if row.batch_id is not None]
    )
    totals = await CartonRepository(session, tenant).totals_for_order(order.id)

    return CartonLabel(
        barcode=carton.barcode,
        packing_order_id=order.id,
        packing_order_code=order.code,
        carton_number=carton.carton_number,
        # The live carton count, not ``order.total_cartons``: a label printed
        # mid-pack would otherwise read "carton 3 of 0" because the header's
        # totals are only frozen at completion.
        total_cartons=totals.carton_count,
        carton_type=CartonType(carton.carton_type),
        customer_id=customer.id,
        customer_code=customer.code,
        customer_name=customer.name,
        sales_order_code=sales_order_code,
        gross_weight_kg=Decimal(carton.gross_weight_kg),
        net_weight_kg=Decimal(carton.net_weight_kg),
        length_cm=carton.length_cm,
        width_cm=carton.width_cm,
        height_cm=carton.height_cm,
        is_sealed=carton.is_sealed,
        sealed_at=carton.sealed_at,
        lines=[
            CartonLabelLine(
                item_id=row.item_id,
                item_code=items[row.item_id].code if row.item_id in items else "",
                item_name=items[row.item_id].name if row.item_id in items else "",
                quantity=Decimal(row.quantity),
                batch_number=(
                    batches[row.batch_id].batch_number
                    if row.batch_id is not None and row.batch_id in batches
                    else None
                ),
                serial_count=len(row.serial_ids),
            )
            for row in contents
        ],
    )


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


async def start_packing(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Put a draft on the bench. Cartons may be opened from here."""
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
        if not lines:
            raise BusinessRuleError(
                f"{order.code} has no lines; there is nothing to pack.",
                details={"order_id": str(order.id)},
            )
        await _transition(
            session, tenant, order, PackingOrderStatus.IN_PROGRESS, reason=reason, repo=repo
        )

    return order, lines


async def complete_packing(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    payload: CompleteRequest,
) -> PackingCompletionResult:
    """Finish packing: freeze the totals and publish the order to dispatch.

    Three things have to hold before an order can be called packed, and all three
    are refusals rather than warnings:

    * there is at least one carton - an order packed into nothing is not packed;
    * every carton is sealed - an open box has not consumed its material and its
      gross weight is not final;
    * every line is fully packed, unless the caller explicitly asked to ship
      short.

    No stock moves here. Completion makes the order available to ``dispatch``;
    the goods leave the building when dispatch posts its own movements
    (database.md section 8).
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    line_repo = PackingOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if PackingOrderStatus(order.status) is not PackingOrderStatus.IN_PROGRESS:
            # Checked before the totals are read so a re-submitted request fails
            # on the state rather than on an arithmetic surprise.
            PACKING_ORDER_LIFECYCLE.assert_transition(
                PackingOrderStatus(order.status), PackingOrderStatus.PACKED
            )

        totals = await carton_repo.totals_for_order(order.id)
        if totals.carton_count == 0:
            raise BusinessRuleError(
                f"{order.code} has no cartons; there is nothing to hand to dispatch.",
                details={"order_id": str(order.id)},
            )
        if totals.open_count:
            raise BusinessRuleError(
                f"{order.code} still has {totals.open_count} unsealed carton(s); seal them "
                "before completing.",
                details={"order_id": str(order.id), "open_cartons": totals.open_count},
            )

        lines = await line_repo.list_for_order(order.id)
        short = [
            line.line_number
            for line in lines
            if to_quantity(Decimal(line.packed_quantity))
            < to_quantity(Decimal(line.quantity_to_pack))
        ]
        if short and not payload.allow_short_pack:
            raise BusinessRuleError(
                f"{order.code} is short-packed on line(s) {', '.join(map(str, short))}. "
                "Pack the balance, or complete with allow_short_pack.",
                details={"order_id": str(order.id), "short_packed_lines": short},
            )

        before = snapshot(order, ORDER_AUDITED_FIELDS)
        order.total_cartons = totals.carton_count
        order.total_gross_weight = totals.gross_weight
        order.total_net_weight = totals.net_weight
        order.packed_date = utcnow()
        order.packed_by_id = tenant.user_id

        await _transition(
            session,
            tenant,
            order,
            PackingOrderStatus.PACKED,
            reason=payload.reason,
            repo=repo,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"{order.code} packed into {totals.carton_count} carton(s), "
                f"{totals.gross_weight} kg gross"
            ),
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(order, ORDER_AUDITED_FIELDS),
            context={
                "reason": payload.reason,
                "allow_short_pack": payload.allow_short_pack,
                "short_packed_lines": short,
            },
        )

    return PackingCompletionResult(
        order=order_detail(order, lines),
        total_cartons=totals.carton_count,
        total_gross_weight=totals.gross_weight,
        total_net_weight=totals.net_weight,
        short_packed_line_numbers=short,
    )


async def hand_over_to_dispatch(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    dispatch_id: uuid.UUID,
    reason: str,
) -> PackingOrder:
    """Claim a packed order for a dispatch.

    The other half of :func:`ready_for_dispatch`, and the reason that feed is
    stable: once a dispatch has taken an order it stops being offered, so two
    dispatchers cannot both plan around the same boxes. Idempotent only for the
    same dispatch - a second, different claim is refused rather than silently
    overwriting the first.
    """
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if order.dispatch_id is not None:
            if order.dispatch_id != dispatch_id:
                raise BusinessRuleError(
                    f"{order.code} has already been taken by another dispatch.",
                    code=ErrorCode.CONFLICT,
                    details={
                        "order_id": str(order.id),
                        "dispatch_id": str(order.dispatch_id),
                    },
                )
            return order

        order.dispatch_id = dispatch_id
        order.handed_over_at = utcnow()
        await _transition(
            session,
            tenant,
            order,
            PackingOrderStatus.HANDED_OVER,
            reason=reason,
            repo=repo,
            action=AuditAction.DISPATCH_CHANGE,
        )

    return order


async def close_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> PackingOrder:
    """Close a handed-over order. Nothing further can happen to it."""
    repo = PackingOrderRepository(session, tenant)
    async with transaction(session):
        order = await _lock_order(repo, order_id)
        await _transition(
            session, tenant, order, PackingOrderStatus.CLOSED, reason=reason, repo=repo
        )
    return order


async def cancel_order(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> PackingOrder:
    """Abandon an order before it is packed.

    Refused once any carton has been sealed even in IN_PROGRESS: packing material
    has been consumed against the order at that point, and a cancelled order that
    ate boxes is a ledger nobody can explain. Unsealed cartons are removed first
    so the lines are given back their packed quantities.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    content_repo = CartonContentRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        current = PackingOrderStatus(order.status)
        if current not in CANCELLABLE_STATUSES:
            PACKING_ORDER_LIFECYCLE.assert_transition(current, PackingOrderStatus.CANCELLED)

        totals = await carton_repo.totals_for_order(order.id)
        if totals.sealed_count:
            raise BusinessRuleError(
                f"{order.code} has {totals.sealed_count} sealed carton(s) and has already "
                "consumed packing material; it cannot be cancelled.",
                details={"order_id": str(order.id), "sealed_cartons": totals.sealed_count},
            )

        cartons = await carton_repo.list_for_order(order.id)
        if cartons:
            contents = await content_repo.list_for_cartons([c.id for c in cartons])
            released: dict[uuid.UUID, Decimal] = {}
            for rows in contents.values():
                for line_id, quantity in _released_quantities(rows).items():
                    released[line_id] = released.get(line_id, ZERO) + quantity
            for carton in cartons:
                await content_repo.delete_for_carton(carton.id)
                await carton_repo.hard_delete(carton)
            await _apply_packed_quantities(
                session, tenant, order, released=released, added=[]
            )

        await _transition(
            session,
            tenant,
            order,
            PackingOrderStatus.CANCELLED,
            reason=reason,
            repo=repo,
            action=AuditAction.CANCEL,
        )

    return order


# -----------------------------------------------------------------------------
# Packing list - queued, never rendered in the request (section 22)
# -----------------------------------------------------------------------------


async def queue_packing_list(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> str:
    """Hand the packing-list PDF to Celery and return the job id.

    The worker is reached by task *name* so the API process never imports the
    worker package, and the broker is imported inside the function so a broker
    that is unreachable fails this request rather than application start-up -
    the same pattern ``mrp`` uses to dispatch a run.
    """
    repo = PackingOrderRepository(session, tenant)
    order = await repo.get_or_404(order_id)

    status = PackingOrderStatus(order.status)
    if status in (PackingOrderStatus.DRAFT, PackingOrderStatus.CANCELLED):
        raise BusinessRuleError(
            f"{order.code} is {order.status}; a packing list is only meaningful once "
            "packing has started.",
            details={"order_id": str(order.id), "status": str(order.status)},
        )

    from src.core.logging import get_request_id
    from src.workers.celery_app import celery_app

    try:
        task = celery_app.send_task(
            PACKING_LIST_TASK_NAME,
            kwargs={
                "packing_order_id": str(order.id),
                "company_id": str(tenant.company_id),
                "user_id": str(tenant.user_id),
                "request_id": get_request_id(),
            },
            queue=PACKING_LIST_QUEUE,
        )
    except Exception as exc:
        log.error("packing.packing_list_dispatch_failed", code=order.code, error=str(exc))
        raise AppError(
            "The packing list could not be queued for background generation.",
            code=ErrorCode.SERVICE_UNAVAILABLE,
            status_code=503,
        ) from exc

    async with transaction(session):
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Queued the packing list for {order.code}",
            user_id=tenant.user_id,
            context={"task": PACKING_LIST_TASK_NAME, "job_id": task.id},
        )

    log.info("packing.packing_list_queued", code=order.code, job_id=task.id)
    return str(task.id)


async def packing_list_data(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> dict[str, Any]:
    """Everything the packing-list renderer needs, in four batched statements.

    Assembled here rather than in the worker so the document the PDF shows and
    the document the API shows are produced by one piece of code. The return is a
    plain mapping of primitives: it crosses a process boundary into a renderer
    that must not hold ORM instances or a session.
    """
    order = await PackingOrderRepository(session, tenant).get_or_404(order_id)
    lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
    cartons = await CartonRepository(session, tenant).list_for_order(order.id)
    contents = await CartonContentRepository(session, tenant).list_for_cartons(
        [carton.id for carton in cartons]
    )

    customer = await CustomerLookupRepository(session, tenant).get_facts(order.customer_id)
    sales_order_code: str | None = None
    if order.sales_order_id is not None:
        header = await SalesOrderLookupRepository(session, tenant).get_header(
            order.sales_order_id
        )
        sales_order_code = header[0] if header is not None else None

    item_ids = {line.item_id for line in lines}
    item_ids.update(row.item_id for rows in contents.values() for row in rows)
    items = await ItemLookupRepository(session, tenant).load(sorted(item_ids))
    batches = await BatchLookupRepository(session, tenant).load(
        [row.batch_id for rows in contents.values() for row in rows if row.batch_id]
    )

    return {
        "code": order.code,
        "status": str(order.status),
        "planned_date": order.planned_date.isoformat(),
        "packed_date": order.packed_date.isoformat() if order.packed_date else None,
        "customer_code": customer.code if customer else "",
        "customer_name": customer.name if customer else "",
        "sales_order_code": sales_order_code,
        "total_cartons": order.total_cartons,
        "total_gross_weight": str(order.total_gross_weight),
        "total_net_weight": str(order.total_net_weight),
        "notes": order.notes,
        "lines": [
            {
                "line_number": line.line_number,
                "item_code": items[line.item_id].code if line.item_id in items else "",
                "item_name": items[line.item_id].name if line.item_id in items else "",
                "quantity_to_pack": str(line.quantity_to_pack),
                "packed_quantity": str(line.packed_quantity),
            }
            for line in lines
        ],
        "cartons": [
            {
                "carton_number": carton.carton_number,
                "barcode": carton.barcode,
                "carton_type": str(carton.carton_type),
                "gross_weight_kg": str(carton.gross_weight_kg),
                "net_weight_kg": str(carton.net_weight_kg),
                "is_sealed": carton.is_sealed,
                "contents": [
                    {
                        "item_code": items[row.item_id].code if row.item_id in items else "",
                        "item_name": items[row.item_id].name if row.item_id in items else "",
                        "quantity": str(row.quantity),
                        "batch_number": (
                            batches[row.batch_id].batch_number
                            if row.batch_id and row.batch_id in batches
                            else None
                        ),
                        "serial_numbers": [str(s) for s in row.serial_ids],
                    }
                    for row in contents.get(carton.id, [])
                ],
            }
            for carton in cartons
        ],
    }


__all__ = [
    "AUDIT_CARTON",
    "AUDIT_PACKING_ORDER",
    "BARCODE_ALPHABET",
    "MOVEMENT_REFERENCE_CARTON",
    "PACKING_LIST_QUEUE",
    "PACKING_LIST_TASK_NAME",
    "PACKING_ORDER_LIFECYCLE",
    "LineFacts",
    "PlannedContent",
    "add_carton",
    "barcode_body",
    "barcode_check_character",
    "build_barcode",
    "build_material_movement",
    "cancel_order",
    "carton_detail",
    "carton_label",
    "close_order",
    "complete_packing",
    "create_order",
    "delete_draft_order",
    "find_cartons_holding_serial",
    "get_carton_detail",
    "get_order_detail",
    "gross_weight_of",
    "hand_over_to_dispatch",
    "list_cartons",
    "list_orders",
    "list_packing_materials",
    "list_ready_for_dispatch",
    "net_weight_of",
    "order_detail",
    "packing_list_data",
    "plan_contents",
    "queue_packing_list",
    "ready_for_dispatch",
    "recompute_packed_quantities",
    "remove_carton",
    "replace_carton_contents",
    "seal_carton",
    "start_packing",
    "update_order",
    "verify_barcode",
]
