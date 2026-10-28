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
* **A sealed carton stays immutable in the normal flow.** The legacy recovery
  exception only handles an unclaimed order whose serial FINAL inspection named
  quantities without units. It returns physically recovered packing material at
  the original issue cost, audits both ledger entries and reopens the carton so
  the order can be cancelled and repacked with actual serial selections.
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
from typing import Any, Protocol, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
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
    CartonScanResult,
    CompleteRequest,
    PackingCompletionResult,
    PackingFromAssemblyCreate,
    PackingMaterialRow,
    PackingOrderCreate,
    PackingOrderDetail,
    PackingOrderFilters,
    PackingOrderLineIn,
    PackingOrderLineRead,
    PackingOrderSummary,
    PackingOrderUpdate,
    PackingSerialSelection,
    ReadyForDispatchRow,
    SealCartonRequest,
    SealResult,
)
from src.modules.packing.serial_tracking import (
    SerialContent,
    assert_named_verdicts,
    assert_selected_contents,
    requires_named_final,
    select_final_units,
)
from src.modules.products.models import ItemGroup
from src.modules.reports import service as reports_service
from src.modules.reports.models import ReportKey
from src.modules.reports.repository import ReportSnapshotRepository

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


class DispatchPlanningPort(Protocol):
    async def open_dispatch_quantities(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        sales_order_id: uuid.UUID,
    ) -> dict[uuid.UUID, Decimal]: ...


class _DispatchPlanningAdapter:
    async def open_dispatch_quantities(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        sales_order_id: uuid.UUID,
    ) -> dict[uuid.UUID, Decimal]:
        from src.modules.dispatch.service import open_dispatch_quantities

        return await open_dispatch_quantities(session, tenant, sales_order_id)


def resolve_dispatch_plan_port() -> DispatchPlanningPort:
    return _DispatchPlanningAdapter()


class DispatchLoadingPort(Protocol):
    async def load_packing_order(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        dispatch_id: uuid.UUID,
        *,
        packing_order_id: uuid.UUID,
        reason: str,
    ) -> None: ...


class _DispatchLoadingAdapter:
    """The consignment side of a hand-over, reached at call time.

    Dispatch imports this module, so the reverse reach is a deferred import -
    the same seam as :class:`_DispatchPlanningAdapter`.
    """

    async def load_packing_order(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        dispatch_id: uuid.UUID,
        *,
        packing_order_id: uuid.UUID,
        reason: str,
    ) -> None:
        from src.modules.dispatch.service import load_packing_order

        await load_packing_order(
            session, tenant, dispatch_id, packing_order_id=packing_order_id, reason=reason
        )


def resolve_dispatch_loading_port() -> DispatchLoadingPort:
    return _DispatchLoadingAdapter()


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
            PackingOrderStatus.IN_PROGRESS,
            permission=Permission.PACKING_MANAGE,
            label="Recover legacy serial packing",
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
        # ASSUMPTION: a consignment cancelled before it left gives its boxes back
        # (docs/assumptions.md §G1-3). The dispatch machine cancels only from
        # NOT_DISPATCHED or READY_FOR_DISPATCH, so nothing in the cartons has
        # shipped: they are packed goods again, and the order returns to the
        # ready-for-dispatch feed for another consignment to claim instead of
        # staying HANDED_OVER to one that will never move. Only
        # ``release_dispatch_claims``, called from inside the dispatch
        # cancellation, takes this edge; a consignment that has left cannot be
        # cancelled, so shipped history never reaches it.
        Transition(
            PackingOrderStatus.HANDED_OVER,
            PackingOrderStatus.PACKED,
            permission=Permission.DISPATCH_UPDATE,
            label="Released by a cancelled dispatch",
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


def order_detail(order: PackingOrder, lines: Sequence[PackingOrderLine]) -> PackingOrderDetail:
    detail = PackingOrderDetail.model_validate(order)
    detail.lines = [PackingOrderLineRead.model_validate(line) for line in lines]
    return detail


async def line_reads(
    session: AsyncSession, tenant: TenantContext, lines: Sequence[PackingOrderLine]
) -> list[PackingOrderLineRead]:
    """Packing-authorized facts in two batched reads, never one lookup per line."""
    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
    inspected = [line.id for line in lines if line.qc_inspection_id is not None]
    reader = resolve_inspection_reader_port()
    inspections = (
        await reader(session, tenant, QUALITY_REFERENCE_PACKING_ORDER, inspected)
        if inspected and reader
        else []
    )
    statuses = {row.id: str(row.status) for row in inspections}
    result = []
    for line in lines:
        data = PackingOrderLineRead.model_validate(line)
        item = items.get(line.item_id)
        if item is not None:
            data.is_serial_tracked = item.is_serial_tracked
            data.is_batch_tracked = item.is_batch_tracked
            data.requires_final_qc = item.requires_final_qc
        data.qc_inspection_status = statuses.get(line.qc_inspection_id)
        result.append(data)
    return result


async def order_read(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    lines: Sequence[PackingOrderLine],
) -> PackingOrderDetail:
    detail = PackingOrderDetail.model_validate(order)
    detail.lines = await line_reads(session, tenant, lines)
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
    exclude_packing_order_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, PackingItem]:
    """Validate a line set and its remaining sales-order demand in batched reads.

    Items must exist and be active. A named sales order line must belong to the
    sales order on the header and be for the same item - a link to either the
    wrong order or the wrong item would corrupt the fulfilment arithmetic the
    sales module does against it, in a way nothing downstream could detect.
    Items, sales lines and current packing/dispatch plans are loaded in fixed
    grouped reads rather than queried once per requested line.
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

    known = await SalesOrderLookupRepository(session, tenant).lock_lines(sales_line_ids)
    for line in lines:
        if line.sales_order_line_id is None:
            continue
        found = known.get(line.sales_order_line_id)
        if found is None:
            raise NotFoundError("Sales order line", line.sales_order_line_id)
        if found.sales_order_id != sales_order_id:
            raise BusinessRuleError(
                "That sales order line belongs to a different sales order.",
                details={"sales_order_line_id": str(line.sales_order_line_id)},
            )
        if found.item_id != line.item_id:
            raise BusinessRuleError(
                "That sales order line is for a different item.",
                details={
                    "sales_order_line_id": str(line.sales_order_line_id),
                    "item_id": str(line.item_id),
                },
            )

    requested_by_line: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        if line.sales_order_line_id is not None:
            requested_by_line[line.sales_order_line_id] = requested_by_line.get(
                line.sales_order_line_id, ZERO
            ) + Decimal(line.quantity_to_pack)

    packing_plans = await PackingOrderLineRepository(
        session, tenant
    ).open_planned_quantities_by_sales_line(
        sales_order_id,
        exclude_packing_order_id=exclude_packing_order_id,
    )
    dispatch_plans = await resolve_dispatch_plan_port().open_dispatch_quantities(
        session, tenant, sales_order_id
    )
    for line_id, requested in requested_by_line.items():
        facts = known[line_id]
        already_packing = packing_plans.get(line_id, ZERO)
        already_dispatch = dispatch_plans.get(line_id, ZERO)
        available = max(
            facts.quantity - facts.dispatched_quantity - already_packing - already_dispatch,
            ZERO,
        )
        if requested > available:
            raise BusinessRuleError(
                f"Packing quantity for sales order line {line_id} exceeds the "
                f"{available} still available after existing packing and dispatch plans.",
                code=ErrorCode.CONFLICT,
                details={
                    "sales_order_line_id": str(line_id),
                    "requested_quantity": str(to_quantity(requested)),
                    "ordered_quantity": str(to_quantity(facts.quantity)),
                    "dispatched_quantity": str(to_quantity(facts.dispatched_quantity)),
                    "packing_planned_quantity": str(to_quantity(already_packing)),
                    "dispatch_planned_quantity": str(to_quantity(already_dispatch)),
                    "available_quantity": str(to_quantity(available)),
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
            if serial.batch_id != content.batch_id:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} does not belong to the carton's batch.",
                    details={"serial_id": str(serial_id), "line_number": content.line_number},
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
    updated = recompute_packed_quantities(lines=_line_facts(rows), released=released, added=added)
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
    return await order_read(session, tenant, order, lines)


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


async def find_carton_by_barcode(
    session: AsyncSession, tenant: TenantContext, barcode: str
) -> CartonScanResult:
    """Resolve a scanned label to its tenant-scoped order and carton record."""
    normalized = barcode.strip().upper()
    carton = await CartonRepository(session, tenant).get_by_barcode(normalized)
    if carton is None:
        raise NotFoundError("Carton", normalized)

    order = await PackingOrderRepository(session, tenant).get_or_404(carton.packing_order_id)
    contents = await CartonContentRepository(session, tenant).list_for_carton(carton.id)
    return CartonScanResult(
        packing_order=PackingOrderSummary.model_validate(order),
        carton=carton_detail(carton, contents),
    )


async def record_carton_scan(
    session: AsyncSession, tenant: TenantContext, barcode: str
) -> CartonScanResult:
    """Resolve a carton and record the successful scan atomically."""
    async with transaction(session):
        result = await find_carton_by_barcode(session, tenant, barcode)
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.SCAN,
            entity_type="carton_scan",
            entity_id=result.carton.id,
            entity_code=result.carton.barcode,
            summary=(
                f"Scanned carton {result.carton.barcode} "
                f"for packing order {result.packing_order.code}"
            ),
            user_id=tenant.user_id,
            context={
                "packing_order_id": str(result.packing_order.id),
                "packing_order_code": result.packing_order.code,
                "carton_number": result.carton.carton_number,
            },
        )
    return result


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
    """ "Which carton contains this serial?" - the question this module exists for.

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
    session: AsyncSession,
    tenant: TenantContext,
    payload: PackingOrderCreate,
    *,
    audit_context: Mapping[str, Any] | None = None,
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Raise a DRAFT packing order with the lines it has to fill.

    Nothing is boxed and nothing is consumed: a draft is a plan for a bench, and
    opening cartons against it before anybody has started would consume packing
    material for work that may never happen.

    The one creation path: the HTTP create, the hand-over from assembly
    (:func:`create_order_from_assembly`) and any later source all land here, so
    validation and the audit row cannot diverge between them. ``audit_context``
    is how a caller records where the order came from on that same row.
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

        warehouses = await WarehouseLookupRepository(session, tenant).load([payload.warehouse_id])
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
            summary=(f"Created {order.code}: pack {len(lines)} line(s) for {customer.code}"),
            user_id=tenant.user_id,
            new_values={
                **snapshot(order, ORDER_AUDITED_FIELDS),
                "customer_code": customer.code,
                "line_count": len(lines),
            },
            context=dict(audit_context) if audit_context else None,
        )

    return order, lines


# -----------------------------------------------------------------------------
# Hand-over from assembly
# -----------------------------------------------------------------------------


class AssemblyHandoverPort(Protocol):
    """The two assembly calls a hand-over makes, in one transaction.

    ``packing_facts`` locks the completed assembly and says what it produced;
    ``mark_packed`` records the packing order that took it. Typed loosely on the
    way back because the facts are assembly's own dataclass, read by attribute.
    """

    async def packing_facts(
        self, session: AsyncSession, tenant: TenantContext, assembly_order_id: uuid.UUID
    ) -> Any: ...

    async def mark_packed(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        assembly_order_id: uuid.UUID,
        *,
        packing_order_id: uuid.UUID,
        reason: str,
    ) -> Any: ...


class AssemblyHandover:
    """Default port: the assembly module, imported when first used."""

    async def packing_facts(
        self, session: AsyncSession, tenant: TenantContext, assembly_order_id: uuid.UUID
    ) -> Any:
        from src.modules.assembly.service import packing_handover_facts

        return await packing_handover_facts(session, tenant, assembly_order_id)

    async def mark_packed(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        assembly_order_id: uuid.UUID,
        *,
        packing_order_id: uuid.UUID,
        reason: str,
    ) -> Any:
        from src.modules.assembly.service import mark_packed

        return await mark_packed(
            session, tenant, assembly_order_id, packing_order_id=packing_order_id, reason=reason
        )


def resolve_assembly_port() -> AssemblyHandoverPort | None:
    """The assembly module's hand-over calls, if it is deployed."""
    try:
        from src.modules.assembly import service as assembly_service
    except ImportError:
        return None
    if not callable(getattr(assembly_service, "packing_handover_facts", None)):
        return None
    return AssemblyHandover()


#: The assembly source that names the sales order a hand-over is packed for -
#: ``assembly.models.AssemblySourceType.SALES_ORDER``, spelled here so packing
#: does not import another module's enum for one comparison.
ASSEMBLY_SOURCE_SALES_ORDER = "SALES_ORDER"


async def create_order_from_assembly(
    session: AsyncSession,
    tenant: TenantContext,
    payload: PackingFromAssemblyCreate,
    *,
    assembly: AssemblyHandoverPort | None = None,
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Raise a packing order for a completed assembly and claim the assembly.

    One transaction: the assembly is locked and checked (completed, produced
    good output, not claimed by another packing order), the packing order is
    raised through :func:`create_order` - the one creation path - and the
    assembly is marked packed against it. A second packer reaching for the same
    assembly waits on its row lock and is then refused before anything is
    created; nothing can leave a packing order without its claim, or a claim
    without its packing order.

    ASSUMPTION: the packing order packs the assembled item from the warehouse
    the output was received into, for the assembled quantity unless a smaller
    one is asked for. Its customer and sales order come from the assembly's
    source when it was raised for a sales order; otherwise the customer must be
    named. The sales-order *line* is not guessed (docs/assumptions.md §E4-8).
    """
    port = assembly if assembly is not None else resolve_assembly_port()
    if port is None:
        raise BusinessRuleError(
            "Assembly is not available in this deployment, so nothing can be packed from it."
        )

    async with transaction(session):
        facts = await port.packing_facts(session, tenant, payload.assembly_order_id)

        source_order_id: uuid.UUID | None = (
            facts.source_id if str(facts.source_type) == ASSEMBLY_SOURCE_SALES_ORDER else None
        )
        if (
            source_order_id is not None
            and payload.sales_order_id is not None
            and payload.sales_order_id != source_order_id
        ):
            raise BusinessRuleError(
                f"{facts.code} was assembled for a different sales order than the one named.",
                details={
                    "assembly_order_id": str(facts.assembly_order_id),
                    "source_sales_order_id": str(source_order_id),
                },
            )
        sales_order_id = payload.sales_order_id or source_order_id

        customer_id = payload.customer_id
        if customer_id is None and sales_order_id is not None:
            header = await SalesOrderLookupRepository(session, tenant).get_header(sales_order_id)
            if header is None:
                raise NotFoundError("Sales order", sales_order_id)
            customer_id = header[1]
        if customer_id is None:
            raise ValidationError(
                f"{facts.code} was not assembled for a sales order; name the customer the "
                "packing order is for.",
                details={"fields": {"customer_id": ["Required for this assembly."]}},
            )

        assembled = to_quantity(Decimal(facts.assembled_quantity))
        quantity = to_quantity(payload.quantity) if payload.quantity is not None else assembled
        if quantity > assembled:
            raise BusinessRuleError(
                f"{facts.code} assembled {assembled}; a packing order cannot take {quantity}.",
                details={"assembled_quantity": str(assembled), "requested": str(quantity)},
            )

        items = await ItemLookupRepository(session, tenant).load([facts.item_id])
        item = items.get(facts.item_id)
        if item is None:
            raise NotFoundError("Item", facts.item_id)

        order, lines = await create_order(
            session,
            tenant,
            PackingOrderCreate(
                customer_id=customer_id,
                warehouse_id=facts.output_warehouse_id,
                planned_date=payload.planned_date or today(),
                sales_order_id=sales_order_id,
                notes=payload.notes or f"Packed from assembly order {facts.code}",
                lines=[
                    PackingOrderLineIn(
                        item_id=facts.item_id,
                        quantity_to_pack=quantity,
                        uom_id=item.base_uom_id,
                    )
                ],
            ),
            audit_context={
                "source": "assembly_order",
                "assembly_order_id": facts.assembly_order_id,
                "assembly_order_code": facts.code,
            },
        )

        await port.mark_packed(
            session,
            tenant,
            facts.assembly_order_id,
            packing_order_id=order.id,
            reason=f"Packing order {order.code} raised from {facts.code}",
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
            if order.sales_order_id is not None:
                header = await SalesOrderLookupRepository(session, tenant).get_header(
                    order.sales_order_id
                )
                if header is None:
                    raise NotFoundError("Sales order", order.sales_order_id)
                if header[1] != order.customer_id:
                    raise BusinessRuleError(
                        f"Sales order {header[0]} belongs to a different customer.",
                        details={"sales_order_id": str(order.sales_order_id)},
                    )
            parsed = [PackingOrderLineIn.model_validate(line) for line in new_lines]
            await _validate_lines(
                session,
                tenant,
                lines=parsed,
                sales_order_id=order.sales_order_id,
                exclude_packing_order_id=order.id,
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
                f"{order.code} is {order.status}; only a draft may be deleted. Cancel it instead.",
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
            summary=(f"Opened carton {carton.carton_number} ({carton.barcode}) on {order.code}"),
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
                f"Carton {carton.barcode} is sealed; what is inside a closed box cannot be edited.",
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

    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
    planned = plan_contents(requests, lines=by_number, items=items)
    assert_selected_contents(
        [
            SerialContent(content.line_id, content.batch_id, content.serial_ids)
            for content in planned
        ],
        lines,
        items,
    )

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

    await _apply_packed_quantities(session, tenant, order, released=released, added=planned)
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
                f"Carton {carton.barcode} is empty; sealing it would consume a box for nothing.",
                details={"carton_id": str(carton.id), "barcode": carton.barcode},
            )

        lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
        items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
        assert_selected_contents(
            [
                SerialContent(row.packing_order_line_id, row.batch_id, row.serial_ids)
                for row in contents
            ],
            lines,
            items,
        )
        contained = {row.packing_order_line_id for row in contents}
        serial_final = [
            line
            for line in lines
            if line.id in contained and requires_named_final(items.get(line.item_id))
        ]
        if serial_final:
            # A rejected serial must be replaceable while its carton is open;
            # sealing before the verdict would permanently freeze rejected units.
            await _assert_final_qc_accepted(session, tenant, order, serial_final, action="seal")

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


async def recover_legacy_carton(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    carton_id: uuid.UUID,
    *,
    reason: str,
) -> CartonDetail:
    """Return a legacy box's original issue and reopen it for audited repacking.

    This exception is restricted to orders with quantity-only serial FINAL QC,
    before dispatch claims them. It never supplies missing historical unit names.
    The operator must physically recover the reusable material, then cancel and
    repack with freshly selected units; shipped history is immutable.
    """
    repo = PackingOrderRepository(session, tenant)
    carton_repo = CartonRepository(session, tenant)
    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if (
            order.status not in (PackingOrderStatus.IN_PROGRESS, PackingOrderStatus.PACKED)
            or order.dispatch_id is not None
        ):
            raise BusinessRuleError(
                "Only active packing orders not claimed by dispatch can recover legacy cartons."
            )
        lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
        items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
        if not any(
            requires_named_final(items.get(line.item_id))
            and line.qc_inspection_id is not None
            and not line.qc_serial_ids
            for line in lines
        ):
            raise BusinessRuleError(
                "Only legacy serial FINAL packing without named QC selections "
                "can recover a sealed carton."
            )
        carton = await carton_repo.get_in_order(order.id, carton_id)
        if carton is None:
            raise NotFoundError("Carton", carton_id)
        if not carton.is_sealed or carton.material_transaction_id is None:
            raise BusinessRuleError(
                "Only a sealed legacy carton can return its original packing material."
            )
        original_id = carton.material_transaction_id
        issue = await carton_repo.material_issue(original_id)
        if (
            issue is None
            or str(issue.transaction_type) != "MATERIAL_ISSUE"
            or issue.reference_type != MOVEMENT_REFERENCE_CARTON
            or issue.reference_id != carton.id
            or issue.item_id != carton.packing_material_item_id
            or issue.warehouse_id != order.warehouse_id
            or -issue.quantity != carton.packing_material_quantity
        ):
            raise BusinessRuleError(
                "The carton has no matching original material issue; "
                "reconcile its ledger before recovery."
            )
        before = snapshot(carton, CARTON_AUDITED_FIELDS)
        posted = await post_movements(
            session,
            tenant,
            [
                MovementRequest(
                    item_id=issue.item_id,
                    warehouse_id=issue.warehouse_id,
                    location_id=issue.location_id,
                    batch_id=issue.batch_id,
                    serial_id=issue.serial_id,
                    quantity=-issue.quantity,
                    unit_cost=issue.unit_cost,
                    transaction_type=TransactionType.MATERIAL_RETURN,
                    reference_type=MOVEMENT_REFERENCE_CARTON,
                    reference_id=carton.id,
                    reference_code=carton.barcode,
                    reason=reason,
                )
            ],
        )
        carton.is_sealed = False
        carton.sealed_at = None
        carton.material_transaction_id = None
        carton_repo.touch(carton)
        if order.status == PackingOrderStatus.PACKED:
            order.packed_date = None
            order.packed_by_id = None
            order.total_cartons = 0
            order.total_gross_weight = ZERO
            order.total_net_weight = ZERO
            await _transition(
                session, tenant, order, PackingOrderStatus.IN_PROGRESS, reason=reason, repo=repo
            )
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_CARTON,
            entity_id=carton.id,
            entity_code=carton.barcode,
            summary=f"Recovered legacy carton {carton.barcode} for named-unit repacking",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(carton, CARTON_AUDITED_FIELDS),
            context={
                "reason": reason,
                "packing_order_id": str(order.id),
                "original_material_issue_id": str(original_id),
                "material_return_id": str(posted[0].id),
            },
        )
        contents = await CartonContentRepository(session, tenant).list_for_carton(carton.id)
    return carton_detail(carton, contents)


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
        header = await SalesOrderLookupRepository(session, tenant).get_header(order.sales_order_id)
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


# =============================================================================
# Final QC seam (WP E5)
# =============================================================================


#: The reference type FINAL inspections name the packing line they cover.
#: Quality owns the inspection; this module reaches it through the reader port,
#: never a join - the line carries only the inspection's id.
QUALITY_REFERENCE_PACKING_ORDER = "packing_order"


class QualityPort(Protocol):
    """What packing asks of the quality module when it is deployed.

    Deliberately narrow: one call raising one FINAL inspection against one
    packing line. Anything wider would couple starting the bench to a module
    that is built independently of it.
    """

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        *,
        item_id: uuid.UUID,
        quantity: Decimal,
        warehouse_id: uuid.UUID,
        reference_type: str,
        reference_id: uuid.UUID,
        reference_code: str,
        batch_id: uuid.UUID | None = None,
        serial_ids: Sequence[uuid.UUID] | None = None,
    ) -> Any: ...


class _SerialInspectionOptions(TypedDict, total=False):
    serial_ids: Sequence[uuid.UUID]
    batch_id: uuid.UUID | None


def _serial_inspection_options(line: PackingOrderLine) -> _SerialInspectionOptions:
    if getattr(line, "qc_serial_ids", None):
        return {"serial_ids": line.qc_serial_ids, "batch_id": line.qc_batch_id}
    return {}


def resolve_quality_port() -> QualityPort | None:
    """The quality module's FINAL inspection factory, if it is deployed.

    Only the *absence* of the module is tolerated. A failure inside it is not
    swallowed: inspections that could not be raised must roll the start back
    with everything else, rather than letting the bench pack goods nobody will
    ever inspect.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    factory = getattr(quality_service, "create_final_inspection", None)
    return factory if callable(factory) else None


class InspectionReaderPort(Protocol):
    """The quality module's read of every inspection a set of lines raised."""

    async def __call__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        reference_type: str,
        reference_id: list[uuid.UUID],
    ) -> Sequence[Any]: ...


def resolve_inspection_reader_port() -> InspectionReaderPort | None:
    """The quality module's ``inspections_for_reference``, if it is deployed.

    Read by the completion gates, which refuse to complete or hand over while
    a line's FINAL inspection is anything short of ACCEPTED.
    """
    try:
        from src.modules.quality import service as quality_service
    except ImportError:
        return None

    reader = getattr(quality_service, "inspections_for_reference", None)
    return reader if callable(reader) else None


async def _raise_line_inspections(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    lines: Sequence[PackingOrderLine],
    quality_port: QualityPort,
) -> int:
    """Raise a FINAL inspection per line whose item requires final QC.

    The goods are reserved for the sales order, not held: the inspection
    covers what the line has to pack, and completion and hand-over are refused
    until it is ACCEPTED. A REJECTED verdict moves the stock to DAMAGED through
    the ordinary decision path, and the line is re-inspected once replacement
    goods are packed (docs/assumptions.md §E5-8).
    """
    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
    raised = 0
    for line in lines:
        item = items.get(line.item_id)
        if item is None or not item.requires_final_qc:
            continue
        if line.qc_inspection_id is not None:
            continue
        quantity = to_quantity(Decimal(line.quantity_to_pack))
        if quantity <= ZERO:
            continue
        inspection = await quality_port(
            session,
            tenant,
            item_id=line.item_id,
            quantity=quantity,
            warehouse_id=order.warehouse_id,
            reference_type=QUALITY_REFERENCE_PACKING_ORDER,
            reference_id=line.id,
            reference_code=order.code,
            **_serial_inspection_options(line),
        )
        line.qc_inspection_id = getattr(inspection, "id", None)
        if line.qc_inspection_id is not None:
            raised += 1
            log.info(
                "packing.final_inspection_raised",
                order_code=order.code,
                line_number=line.line_number,
                inspection_id=str(line.qc_inspection_id),
            )
    await session.flush()
    return raised


async def _assert_final_qc_accepted(
    session: AsyncSession,
    tenant: TenantContext,
    order: PackingOrder,
    lines: Sequence[PackingOrderLine],
    *,
    action: str,
) -> None:
    """Refuse completion or hand-over while a FINAL inspection is not ACCEPTED.

    Every line carrying an inspection must point at an ACCEPTED one. A REJECTED
    verdict names the line it blocks: the rejected stock has moved to DAMAGED,
    so the operator repacks replacement goods and re-inspects the line, which
    is what unblocks the order. Without the quality module there is nothing to
    check against - but starting already refuses that combination, so reaching
    here with inspections and no reader means the module went away mid-order,
    which is refused loudly rather than waved through.
    """
    inspected = [line for line in lines if line.qc_inspection_id is not None]
    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
    if not inspected:
        assert_named_verdicts(lines, items, {})
        return
    reader = resolve_inspection_reader_port()
    if reader is None:
        raise BusinessRuleError(
            f"{order.code} cannot {action}: its FINAL inspections cannot be read.",
            details={"order_code": order.code},
        )
    verdicts = await reader(
        session,
        tenant,
        QUALITY_REFERENCE_PACKING_ORDER,
        [line.id for line in inspected],
    )
    by_id = {getattr(verdict, "id", None): verdict for verdict in verdicts}
    assert_named_verdicts(lines, items, by_id)
    if any(requires_named_final(items.get(line.item_id)) for line in lines):
        contents = await CartonContentRepository(session, tenant).list_for_order(order.id)
        assert_selected_contents(
            [
                SerialContent(row.packing_order_line_id, row.batch_id, row.serial_ids)
                for row in contents
            ],
            lines,
            items,
        )
    blocking = [
        line
        for line in inspected
        if str(getattr(by_id.get(line.qc_inspection_id), "result", "")) != "ACCEPTED"
    ]
    if blocking:
        numbers = ", ".join(str(line.line_number) for line in blocking)
        raise BusinessRuleError(
            f"{order.code} cannot {action}: line(s) {numbers} still owe an ACCEPTED "
            "final inspection. Decide the inspections, or repack and re-inspect a "
            "rejected line.",
            details={
                "order_code": order.code,
                "blocked_line_numbers": [line.line_number for line in blocking],
                "blocked_inspection_ids": [str(line.qc_inspection_id) for line in blocking],
            },
        )


async def start_packing(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    reason: str,
    serial_selections: Sequence[PackingSerialSelection] = (),
) -> tuple[PackingOrder, list[PackingOrderLine]]:
    """Put a draft on the bench. Cartons may be opened from here."""
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        PACKING_ORDER_LIFECYCLE.assert_transition(
            PackingOrderStatus(order.status), PackingOrderStatus.IN_PROGRESS
        )
        lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
        if not lines:
            raise BusinessRuleError(
                f"{order.code} has no lines; there is nothing to pack.",
                details={"order_id": str(order.id)},
            )
        # Final QC is fail-closed: a line whose item requires it starts no
        # bench without its FINAL inspection, because completing that bench
        # without one would be the gate waving through goods nobody inspected.
        items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
        select_final_units(lines, items, serial_selections)
        quality_port = resolve_quality_port()
        if quality_port is not None:
            raised = await _raise_line_inspections(session, tenant, order, lines, quality_port)
        else:
            raised = 0
            items = await ItemLookupRepository(session, tenant).load(
                [line.item_id for line in lines]
            )
            gated = [
                line.line_number
                for line in lines
                if (found := items.get(line.item_id)) is not None and found.requires_final_qc
            ]
            if gated:
                raise BusinessRuleError(
                    f"{order.code} cannot start: line(s) {', '.join(map(str, gated))} "
                    "require final QC and the quality module is not available.",
                    details={"order_id": str(order.id), "blocked_line_numbers": gated},
                )
        await _transition(
            session, tenant, order, PackingOrderStatus.IN_PROGRESS, reason=reason, repo=repo
        )
        if raised:
            await record_audit(
                session,
                company_id=tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_PACKING_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                summary=(f"{order.code} raised {raised} FINAL inspection(s) on start"),
                user_id=tenant.user_id,
                new_values={
                    "final_inspections_raised": raised,
                    "final_qc_serial_selections": [
                        {
                            "line_id": str(line.id),
                            "serial_ids": line.qc_serial_ids,
                            "batch_id": line.qc_batch_id,
                        }
                        for line in lines
                        if line.qc_serial_ids
                    ],
                },
                context={"reason": reason},
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

        await _assert_final_qc_accepted(session, tenant, order, lines, action="complete")

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
        order=await order_read(session, tenant, order, lines),
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
    """Load a packed order onto the consignment taking it - the hand-over a person asks for.

    ``POST /packing/{id}/hand-over`` and the packing screen's "Hand to dispatch"
    both land here, and the work is the dispatch module's, because the
    consignment is the document being loaded:
    :func:`src.modules.dispatch.service.load_packing_order` reads it through its
    tenant-scoped repository under its row lock, refuses one that does not exist
    in this company, has left, was cancelled or belongs to another customer or
    sales order, claims this order through :func:`record_dispatch_claim` exactly
    as the dispatch create path does, and matches the sealed cartons to the
    consignment's lines. This used to store whatever id it was given in a bare
    column and tell the consignment nothing.
    """
    await resolve_dispatch_loading_port().load_packing_order(
        session, tenant, dispatch_id, packing_order_id=order_id, reason=reason
    )
    return await PackingOrderRepository(session, tenant).get_or_404(order_id)


async def record_dispatch_claim(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    *,
    dispatch_id: uuid.UUID,
    reason: str,
) -> PackingOrder:
    """Record a consignment's claim on a packed order: the packing side of a hand-over.

    The other half of :func:`ready_for_dispatch`, and the reason that feed is
    stable: once a dispatch has taken an order it stops being offered, so two
    dispatchers cannot both plan around the same boxes. Idempotent only for the
    same dispatch - a second, different claim is refused rather than silently
    overwriting the first.

    Only the dispatch module calls this, inside its own transaction and holding
    the consignment's row lock, after it has read and validated the consignment
    (``dispatch.service._claim_packing_order``). It does not look the dispatch up
    itself; a caller that has not is what :func:`hand_over_to_dispatch` exists to
    prevent.
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

        lines = await PackingOrderLineRepository(session, tenant).list_for_order(order.id)
        await _assert_final_qc_accepted(session, tenant, order, lines, action="hand over")

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


async def release_dispatch_claims(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, *, reason: str
) -> list[PackingOrder]:
    """Give a cancelled consignment's boxes back to the packing dock.

    Called by the dispatch module from inside its cancellation, holding the
    consignment's row lock - consignment first, then the orders, the same order
    every hand-over takes them in. Each order still HANDED_OVER to that
    consignment returns to PACKED through the state machine with its claim
    cleared, so it is back on :func:`ready_for_dispatch` for another consignment
    (docs/assumptions.md §G1-3). An order closed by hand in the meantime is left
    as it is.

    The transitions are asserted and their audit rows added without a round trip
    each, then written in one flush: the set is the orders loaded onto one lorry,
    normally one.
    """
    repo = PackingOrderRepository(session, tenant)

    async with transaction(session):
        orders = await repo.lock_handed_over_to(dispatch_id)
        for order in orders:
            current = PackingOrderStatus(order.status)
            PACKING_ORDER_LIFECYCLE.assert_transition(current, PackingOrderStatus.PACKED)
            order.status = PackingOrderStatus.PACKED
            order.dispatch_id = None
            order.handed_over_at = None
            repo.touch(order)
            # ``flush=False`` adds the row without a statement; the one flush
            # below writes the whole set inside this transaction.
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_PACKING_ORDER,
                entity_id=order.id,
                entity_code=order.code,
                from_status=str(current),
                to_status=str(PackingOrderStatus.PACKED),
                user_id=tenant.user_id,
                action=AuditAction.DISPATCH_CHANGE,
                reason=reason,
                flush=False,
            )
        if orders:
            await session.flush()

    return orders


async def reinspect_line(
    session: AsyncSession,
    tenant: TenantContext,
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    reason: str,
    serial_ids: Sequence[uuid.UUID] = (),
    batch_id: uuid.UUID | None = None,
) -> PackingOrderLine:
    """Raise a fresh FINAL inspection for a rejected packing line.

    The replace path for a REJECTED verdict: the rejected stock has moved to
    DAMAGED, so the operator removes the bad cartons and packs replacement
    goods, and this raises the new inspection that covers them. Only a
    REJECTED line can be re-inspected - anything else already has a verdict
    the gates understand, and raising beside it would leave two inspections
    over one line with no rule for which one the gate reads.
    """
    repo = PackingOrderRepository(session, tenant)
    line_repo = PackingOrderLineRepository(session, tenant)

    async with transaction(session):
        order = await _lock_order(repo, order_id)
        if PackingOrderStatus(order.status) is not PackingOrderStatus.IN_PROGRESS:
            raise BusinessRuleError(
                f"{order.code} is {order.status}; lines can only be re-inspected while "
                "the order is being packed.",
                details={"order_id": str(order.id), "status": str(order.status)},
            )
        lines = await line_repo.list_for_order(order.id)
        line = next((row for row in lines if row.id == line_id), None)
        if line is None:
            raise NotFoundError("Packing order line", line_id)
        if line.qc_inspection_id is None:
            raise BusinessRuleError(
                f"Line {line.line_number} of {order.code} has no FINAL inspection; there "
                "is nothing to replace.",
                details={"order_id": str(order.id), "line_id": str(line.id)},
            )

        reader = resolve_inspection_reader_port()
        current = None
        if reader is not None:
            found = await reader(session, tenant, QUALITY_REFERENCE_PACKING_ORDER, [line.id])
            current = next(
                (row for row in found if getattr(row, "id", None) == line.qc_inspection_id),
                None,
            )
        if current is None or str(getattr(current, "result", "")) != "REJECTED":
            raise BusinessRuleError(
                f"Line {line.line_number} of {order.code} can only be re-inspected once "
                "its FINAL inspection is REJECTED.",
                details={
                    "order_id": str(order.id),
                    "line_id": str(line.id),
                    "inspection_id": str(line.qc_inspection_id),
                },
            )

        quality_port = resolve_quality_port()
        if quality_port is None:
            raise BusinessRuleError(
                f"Line {line.line_number} of {order.code} cannot be re-inspected: the "
                "quality module is not available.",
                details={"order_id": str(order.id), "line_id": str(line.id)},
            )
        quantity = to_quantity(Decimal(line.quantity_to_pack))
        items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
        if requires_named_final(items.get(line.item_id)):
            if not serial_ids:
                raise BusinessRuleError(
                    "Select the replacement serial units for FINAL re-inspection."
                )
            sealed = await CartonContentRepository(session, tenant).list_for_order(
                order.id, sealed_only=True
            )
            if any(row.packing_order_line_id == line.id for row in sealed):
                raise BusinessRuleError(
                    "A sealed carton cannot be assigned replacement serials. "
                    "Recover eligible legacy cartons, then cancel and repack "
                    "the replacement units.",
                    details={"line_id": str(line.id)},
                )
            select_final_units(
                lines,
                items,
                [
                    PackingSerialSelection(
                        line_id=line.id, serial_ids=list(serial_ids), batch_id=batch_id
                    )
                ],
            )
        elif serial_ids or batch_id is not None:
            raise BusinessRuleError(
                "Serial selections belong only to lines requiring serial FINAL QC."
            )
        inspection = await quality_port(
            session,
            tenant,
            item_id=line.item_id,
            quantity=quantity,
            warehouse_id=order.warehouse_id,
            reference_type=QUALITY_REFERENCE_PACKING_ORDER,
            reference_id=line.id,
            reference_code=order.code,
            **_serial_inspection_options(line),
        )
        previous = line.qc_inspection_id
        line.qc_inspection_id = getattr(inspection, "id", None)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=(
                f"{order.code} line {line.line_number} re-inspected: "
                f"{getattr(inspection, 'code', '?')} replaces a REJECTED verdict"
            ),
            user_id=tenant.user_id,
            new_values={
                "line_id": str(line.id),
                "previous_inspection_id": str(previous),
                "inspection_id": str(line.qc_inspection_id),
                "serial_ids": line.qc_serial_ids,
                "batch_id": line.qc_batch_id,
            },
            context={"reason": reason},
        )

    return line


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

        await _withdraw_order_inspections(session, tenant, order.id, reason=reason)

        cartons = await carton_repo.list_for_order(order.id)
        if cartons:
            carton_ids = [c.id for c in cartons]
            contents = await content_repo.list_for_cartons(carton_ids)
            released: dict[uuid.UUID, Decimal] = {}
            for rows in contents.values():
                for line_id, quantity in _released_quantities(rows).items():
                    released[line_id] = released.get(line_id, ZERO) + quantity
            # Two statements for the whole set, not two per carton: the order's
            # row lock is held for the duration, and a fifty-carton order must
            # not turn into a hundred round-trips under it.
            await content_repo.delete_for_cartons(carton_ids)
            await carton_repo.bulk_delete(carton_ids)
            await _apply_packed_quantities(session, tenant, order, released=released, added=[])

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


async def _withdraw_order_inspections(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID, *, reason: str
) -> None:
    lines = await PackingOrderLineRepository(session, tenant).list_for_order(order_id)
    inspected = [line.id for line in lines if line.qc_inspection_id is not None]
    if inspected:
        from src.modules.quality.withdrawal import withdraw_packing_inspections

        await withdraw_packing_inspections(session, tenant, inspected, reason=reason)


# -----------------------------------------------------------------------------
# Packing list - queued, never rendered in the request (section 22)
# -----------------------------------------------------------------------------


def packing_list_parameters(order: PackingOrder) -> dict[str, Any]:
    """What a packing-list job is a render *of*, as stored on its job row.

    The order's ``updated_at`` is part of it so the hash - and the row - say
    which version of the order the PDF describes. A packing list is never reused
    across requests (cartons change between two clicks), so the hash exists to
    identify the artefact in a support conversation, not to deduplicate jobs.
    """
    return {
        "report_key": str(ReportKey.PACKING_LIST),
        "packing_order_id": str(order.id),
        "packing_order_code": order.code,
        "as_of": order.updated_at.isoformat(),
    }


async def queue_packing_list(
    session: AsyncSession, tenant: TenantContext, order_id: uuid.UUID
) -> str:
    """Hand the packing-list PDF to Celery and return the job id.

    The job id is a ``report_snapshots`` row, not the Celery task id: the row is
    what ``GET /reports/jobs/{id}`` reads, what the worker marks RUNNING,
    COMPLETED or FAILED, and what carries the ``document_id`` the client
    downloads. A bare task id can be resolved by nothing in the API. The row is
    created and audited in one transaction before the task is published, so a
    broker that refuses the task can mark the job FAILED rather than leave the
    client polling a job that never existed.
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

    snapshots = ReportSnapshotRepository(session, tenant)
    async with transaction(session):
        job = reports_service.new_job_snapshot(
            ReportKey.PACKING_LIST, packing_list_parameters(order), now=utcnow()
        )
        snapshots.add(job)
        await session.flush()
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.JOB_STARTED,
            entity_type=AUDIT_PACKING_ORDER,
            entity_id=order.id,
            entity_code=order.code,
            summary=f"Queued the packing list for {order.code}",
            user_id=tenant.user_id,
            context={"task": PACKING_LIST_TASK_NAME, "job_id": str(job.id)},
        )

    await reports_service.dispatch_job(
        session,
        tenant,
        job,
        task_name=PACKING_LIST_TASK_NAME,
        queue=PACKING_LIST_QUEUE,
        task_kwargs={"packing_order_id": str(order.id)},
        subject="The packing list",
    )

    log.info("packing.packing_list_queued", code=order.code, job_id=str(job.id))
    return str(job.id)


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
        header = await SalesOrderLookupRepository(session, tenant).get_header(order.sales_order_id)
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
    "ASSEMBLY_SOURCE_SALES_ORDER",
    "AUDIT_CARTON",
    "AUDIT_PACKING_ORDER",
    "BARCODE_ALPHABET",
    "MOVEMENT_REFERENCE_CARTON",
    "PACKING_LIST_QUEUE",
    "PACKING_LIST_TASK_NAME",
    "PACKING_ORDER_LIFECYCLE",
    "AssemblyHandover",
    "AssemblyHandoverPort",
    "DispatchLoadingPort",
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
    "create_order_from_assembly",
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
    "packing_list_parameters",
    "plan_contents",
    "queue_packing_list",
    "ready_for_dispatch",
    "recompute_packed_quantities",
    "record_dispatch_claim",
    "reinspect_line",
    "release_dispatch_claims",
    "remove_carton",
    "replace_carton_contents",
    "resolve_assembly_port",
    "resolve_dispatch_loading_port",
    "resolve_inspection_reader_port",
    "resolve_quality_port",
    "seal_carton",
    "start_packing",
    "update_order",
    "verify_barcode",
]
