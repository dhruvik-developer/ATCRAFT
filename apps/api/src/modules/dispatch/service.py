"""Dispatch: sending goods, and knowing where they are afterwards.

Master rules sections 22, 23 and 72. Six decisions govern everything below.

**Exactly one action moves stock.** ``dispatch_consignment`` is the only function
here that touches inventory. It consumes the reservation the sales order was
holding through ``allocation.consume`` - which lowers ``reserved_quantity`` and
posts the ``DISPATCH`` movement in one step (database.md section 8) - and posts
the movements for anything not reserved through ``inventory.post_movements``.
Both happen inside one transaction with the status change, the sales-order
fulfilment arithmetic and the audit rows. There is no code path that ships goods
without writing all of them.

**Partial dispatch is the ordinary case, not an exception** (section 72). A line
for 100 that ships 40 leaves a balance of 60 open, the order becomes
``PARTIALLY_DISPATCHED``, and the next consignment picks up where this one
stopped. The arithmetic that decides all of that - :func:`dispatch_balance` and
:func:`sales_order_status_path` - is pure and unit-tested without a database,
because getting it wrong means either shipping twice or telling a customer their
order is complete when it is not.

**The sales order's status is walked, not assigned.** The sales lifecycle has no
``ALLOCATED -> DISPATCHED`` edge: a fully-shipped order passes through
``PARTIALLY_DISPATCHED`` on the way. :func:`sales_order_status_path` returns the
sequence and every step goes through the sales module's own
``SALES_ORDER_LIFECYCLE``, so this module never writes a status the sales module
would reject.

**Delay is derived on read.** A consignment is late when it is on the road past
its promised date. Nothing stores that as a flag - see the models docstring -
and :func:`scan_delays` is the scheduled sweep that turns the derived answer into
an alert somebody actually sees.

**Serial-tracked goods cannot be dispatched against a reservation.**
``allocation.consume`` posts one movement per call and has nowhere to name a
serial, while the inventory engine requires a serial on every movement of a
serial-tracked item and a quantity of exactly one. The two contracts cannot both
be honoured, so the combination is refused with an explicit message rather than
posted in a way that would leave either ``reserved_quantity`` or the serial
register wrong. This is the same refusal, for the same reason, as
docs/assumptions.md section 20 makes for serial-tracked goods receipts. A
serial-tracked line with no reservation dispatches normally, one movement per
unit.

**Tracking events are the only way into the delivered half of the lifecycle.**
``DELIVERED`` onwards describes what the *receiver* did, which we learn by being
told. Every one of those statuses is therefore reached by recording an event, and
the event is written whether or not the status moved - a position update with no
status change is exactly as much a fact as an arrival.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.codes import DocTypes, next_document_number
from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ErrorCode, NotFoundError
from src.core.logging import get_logger
from src.core.pagination import CursorParams, PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import CursorPagination, OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.allocation import service as allocation
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.dispatch.models import (
    DELIVERED_STATUSES,
    EDITABLE_STATUSES,
    IN_FLIGHT_STATUSES,
    MAX_LINES_PER_DISPATCH,
    Dispatch,
    DispatchLine,
    DispatchStatus,
    DispatchTrackingEvent,
    DispatchType,
    TrackingEventSource,
)
from src.modules.dispatch.repository import (
    GRN_OUTSTANDING_STATUSES,
    BatchLookupRepository,
    CartonLookupRepository,
    CustomerLookupRepository,
    DispatchItem,
    DispatchLineRepository,
    DispatchRepository,
    DispatchTrackingEventRepository,
    DocumentLookupRepository,
    ItemLookupRepository,
    LedgerLookupRepository,
    PackingOrderLookupRepository,
    SalesOrderLineRepository,
    SalesOrderRepository,
    SerialLookupRepository,
    TransporterLookupRepository,
    VendorLookupRepository,
    WarehouseLookupRepository,
    mark_delay_notified,
    scan_delayed_across_tenants,
)
from src.modules.dispatch.schemas import (
    MAX_DELAY_SCAN,
    DelayedDispatchRow,
    DelayScanResult,
    DeliveryConfirmation,
    DispatchCreate,
    DispatchDetail,
    DispatchFilters,
    DispatchLineIn,
    DispatchLineRead,
    DispatchRequest,
    DispatchResult,
    DispatchSummary,
    DispatchUpdate,
    InTransitRequest,
    SalesOrderLineReference,
    TrackingBoardRow,
    TrackingEventIn,
    TrackingEventRead,
    duplicate_sales_order_line_ids,
)
from src.modules.inventory.models import SerialStatus, StockState, TransactionType
from src.modules.inventory.service import MovementRequest, post_movements
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify_role
from src.modules.packing import service as packing
from src.modules.packing.models import PackingOrderStatus
from src.modules.sales.models import SalesOrderLine, SalesOrderStatus
from src.modules.sales.service import SALES_ORDER_LIFECYCLE, line_status_for

log = get_logger(__name__)

AUDIT_DISPATCH = "dispatch"
AUDIT_SALES_ORDER = "sales_order"

#: What the ledger rows this module posts point back at. A bare string matching
#: ``inventory_transactions.reference_type``: the ledger outlives its source, so
#: it holds a type and an id rather than a foreign key (database.md section 4.2).
#: The index on ``(company_id, reference_type, reference_id)`` is what makes
#: "what did this consignment post?" one lookup.
MOVEMENT_REFERENCE_DISPATCH = "dispatch"

#: Refusal text for the one combination the two upstream contracts cannot both
#: satisfy - see the module docstring.
SERIAL_RESERVATION_MESSAGE = (
    "{item_code} is serial tracked and this line is covered by a stock reservation. "
    "A reservation is consumed as one movement, but a serial-tracked movement must name "
    "exactly one unit, so the two cannot be reconciled. Release the reservation and "
    "dispatch the line against free stock, or register the units through inventory first."
)

#: Roles told when a consignment misses its promised delivery date. Resolved from
#: the role assignment at the moment of the sweep, so the list stays correct as
#: people join and leave (see ``notifications.service.notify_role``).
DELAY_ALERT_ROLES: tuple[str, ...] = ("DISPATCH_MANAGER", "PLANT_MANAGER", "ADMIN")

#: Header fields snapshotted around an edit so the audit diff names what moved
#: rather than reprinting the whole row.
DISPATCH_AUDITED_FIELDS = [
    "code",
    "dispatch_type",
    "status",
    "sales_order_id",
    "packing_order_id",
    "customer_id",
    "vendor_id",
    "warehouse_id",
    "dispatch_date",
    "dispatched_at",
    "expected_delivery_date",
    "actual_delivery_date",
    "transporter_id",
    "courier_name",
    "transport_name",
    "tracking_id",
    "lr_number",
    "vehicle_number",
    "driver_name",
    "driver_mobile",
    "total_boxes",
    "total_weight_kg",
    "freight_amount",
    "freight_paid_by",
    "eway_bill_number",
    "invoice_document_id",
    "delivery_confirmed_by",
    "delivery_note",
    "delay_reason",
    "notes",
]

#: Transport fields an explicit ``null`` in an update body legitimately clears.
#: Every other field on the payload either backs a NOT NULL column or is a value
#: nobody clears by accident, so a null there is a client mistake rather than an
#: instruction - see :func:`update_dispatch`.
CLEARABLE_UPDATE_FIELDS = frozenset(
    {
        "courier_name",
        "transport_name",
        "tracking_id",
        "lr_number",
        "vehicle_number",
        "driver_name",
        "driver_mobile",
        "freight_paid_by",
        "eway_bill_number",
        "invoice_document_id",
        "transporter_id",
        "expected_delivery_date",
        "notes",
    }
)


# -----------------------------------------------------------------------------
# State machine - the only thing permitted to move dispatches.status
# -----------------------------------------------------------------------------

DISPATCH_LIFECYCLE: StateMachine[DispatchStatus] = StateMachine(
    entity="Dispatch",
    initial=DispatchStatus.NOT_DISPATCHED,
    transitions=(
        Transition(
            DispatchStatus.NOT_DISPATCHED,
            DispatchStatus.READY_FOR_DISPATCH,
            permission=Permission.DISPATCH_UPDATE,
            label="Mark ready",
        ),
        Transition(
            DispatchStatus.READY_FOR_DISPATCH,
            DispatchStatus.DISPATCHED,
            permission=Permission.DISPATCH_CREATE,
            label="Dispatch",
            # Consumes the reservations and posts the DISPATCH movements. The
            # goods have left the building; nothing here is reversible.
            is_irreversible=True,
        ),
        Transition(
            DispatchStatus.DISPATCHED,
            DispatchStatus.IN_TRANSIT,
            permission=Permission.DISPATCH_UPDATE,
            label="In transit",
        ),
        # Delivery is reachable from both: a same-day local drop is confirmed
        # without anybody having recorded an in-transit scan first, and refusing
        # that would push the confirmation into a comment field.
        Transition(
            DispatchStatus.DISPATCHED,
            DispatchStatus.DELIVERED,
            permission=Permission.DISPATCH_CONFIRM_DELIVERY,
            label="Confirm delivery",
            is_irreversible=True,
        ),
        Transition(
            DispatchStatus.IN_TRANSIT,
            DispatchStatus.DELIVERED,
            permission=Permission.DISPATCH_CONFIRM_DELIVERY,
            label="Confirm delivery",
            is_irreversible=True,
        ),
        # The receiver's booking-in sequence (database.md section 7). We do not
        # perform these; we are told about them, which is why each is reached by
        # recording a tracking event.
        Transition(
            DispatchStatus.DELIVERED,
            DispatchStatus.QC_PENDING,
            permission=Permission.DISPATCH_UPDATE,
            label="Awaiting inspection",
        ),
        Transition(
            DispatchStatus.DELIVERED,
            DispatchStatus.RECEIVED,
            permission=Permission.DISPATCH_UPDATE,
            label="Accepted without inspection",
        ),
        Transition(
            DispatchStatus.QC_PENDING,
            DispatchStatus.RECEIVED,
            permission=Permission.DISPATCH_UPDATE,
            label="Received",
        ),
        Transition(
            DispatchStatus.RECEIVED,
            DispatchStatus.QC_COMPLETED,
            permission=Permission.DISPATCH_UPDATE,
            label="Inspection complete",
        ),
        Transition(
            DispatchStatus.QC_COMPLETED,
            DispatchStatus.GRN_COMPLETED,
            permission=Permission.DISPATCH_UPDATE,
            label="Booked in",
        ),
        # ASSUMPTION: section 7 lists no cancelled state. Cancellation is
        # permitted only from the two statuses in which nothing has moved - past
        # DISPATCHED the ledger records goods that left, and a document claiming
        # never to have existed would contradict it.
        Transition(
            DispatchStatus.NOT_DISPATCHED,
            DispatchStatus.CANCELLED,
            permission=Permission.DISPATCH_UPDATE,
            label="Cancel",
        ),
        Transition(
            DispatchStatus.READY_FOR_DISPATCH,
            DispatchStatus.CANCELLED,
            permission=Permission.DISPATCH_UPDATE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({DispatchStatus.GRN_COMPLETED, DispatchStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Pure arithmetic - unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineBalance:
    """What one sales-order line owes after a consignment takes part of it.

    Computed before anything is written so the decision can be tested on its own,
    and so a refusal is a typed business error naming the numbers rather than an
    integrity error from a CHECK constraint the user has never heard of.
    """

    ordered: Decimal
    dispatched_before: Decimal
    dispatching_now: Decimal
    dispatched_after: Decimal
    balance: Decimal

    @property
    def is_complete(self) -> bool:
        return self.balance <= ZERO

    @property
    def is_partial(self) -> bool:
        return self.dispatched_after > ZERO and self.balance > ZERO


def dispatch_balance(
    *,
    ordered: Decimal,
    dispatched_before: Decimal,
    dispatching_now: Decimal,
    line_label: str = "line",
) -> LineBalance:
    """How much of an order line is left after this consignment ships part of it.

    The rule master rules section 72 turns on: shipping less than was ordered is
    normal and leaves the balance open; shipping *more* than was ordered is
    refused rather than clamped, because the difference is either a mis-keyed
    quantity or goods nobody ordered and both need a person.
    """
    ordered_q = to_quantity(ordered)
    before_q = to_quantity(dispatched_before)
    now_q = to_quantity(dispatching_now)

    if now_q <= ZERO:
        raise BusinessRuleError(
            f"{line_label}: a dispatch quantity must be greater than zero.",
            details={"quantity": str(now_q)},
        )

    after = to_quantity(before_q + now_q)
    if after > ordered_q:
        raise BusinessRuleError(
            f"{line_label}: {ordered_q} were ordered and {before_q} already dispatched, so "
            f"{now_q} cannot be sent - only {to_quantity(ordered_q - before_q)} remain.",
            details={
                "ordered": str(ordered_q),
                "already_dispatched": str(before_q),
                "requested": str(now_q),
                "remaining": str(to_quantity(ordered_q - before_q)),
            },
        )

    return LineBalance(
        ordered=ordered_q,
        dispatched_before=before_q,
        dispatching_now=now_q,
        dispatched_after=after,
        balance=to_quantity(ordered_q - after),
    )


def sales_order_status_path(
    current: SalesOrderStatus, *, fully_dispatched: bool
) -> tuple[SalesOrderStatus, ...]:
    """The statuses a sales order must be walked through after a consignment.

    The sales lifecycle has no ``ALLOCATED -> DISPATCHED`` edge: an order shipped
    complete in one go still passes through ``PARTIALLY_DISPATCHED``. Returning
    the whole path - rather than assigning the destination - keeps every step
    inside the sales module's own state machine, so this module can never write a
    status that machine would reject, and every hop gets its own audit row.

    An empty tuple means the order is already where it should be: a second
    partial consignment against an order that is already ``PARTIALLY_DISPATCHED``
    changes the quantities, not the status.
    """
    target = (
        SalesOrderStatus.DISPATCHED if fully_dispatched else (SalesOrderStatus.PARTIALLY_DISPATCHED)
    )
    if current is target:
        return ()
    if current is SalesOrderStatus.ALLOCATED and target is SalesOrderStatus.DISPATCHED:
        return (SalesOrderStatus.PARTIALLY_DISPATCHED, SalesOrderStatus.DISPATCHED)
    return (target,)


def _dispatchable_order_statuses() -> frozenset[SalesOrderStatus]:
    """Sales-order statuses a consignment may leave from.

    Derived from the sales module's own lifecycle rather than listed by hand, so
    the two cannot drift: an order can be dispatched when its next hop is
    ``PARTIALLY_DISPATCHED``, or when it is already there and a further
    consignment only changes the quantities.
    """
    return frozenset(
        status
        for status in SalesOrderStatus
        if status is SalesOrderStatus.PARTIALLY_DISPATCHED
        or SALES_ORDER_LIFECYCLE.can_transition(status, SalesOrderStatus.PARTIALLY_DISPATCHED)
    )


#: ``ALLOCATED`` and ``PARTIALLY_DISPATCHED`` under database.md section 7.
DISPATCHABLE_ORDER_STATUSES: frozenset[SalesOrderStatus] = _dispatchable_order_statuses()


def assert_order_dispatchable(order_code: str, order_status: SalesOrderStatus) -> None:
    """Refuse, before any stock moves, an order that is not ready to leave.

    Without this the refusal came from :func:`_advance_sales_order`, after the
    reservations had been consumed and the movements posted - all rolled back
    by the transaction, so nothing was harmed, but the operator was told
    "Sales order cannot move from 'CONFIRMED' to 'PARTIALLY_DISPATCHED'"
    after filling in the whole consignment, with no hint that allocation was
    the missing step. The message names the actual remedy.
    """
    if order_status in DISPATCHABLE_ORDER_STATUSES:
        return
    raise BusinessRuleError(
        f"Sales order {order_code} is {str(order_status).replace('_', ' ').lower()} and cannot "
        "be dispatched yet: it must be fully allocated before a consignment can leave.",
        code=ErrorCode.INVALID_STATE_TRANSITION,
        details={
            "sales_order_code": order_code,
            "sales_order_status": str(order_status),
            "dispatchable_statuses": sorted(str(s) for s in DISPATCHABLE_ORDER_STATUSES),
        },
    )


def is_delayed(
    *,
    status: DispatchStatus,
    expected_delivery_date: date | None,
    as_of: date | None = None,
) -> bool:
    """Is this consignment late?

    One definition, matching the SQL predicate in the repository exactly: on the
    road, promised a date, and that date has passed. A consignment with no
    promised date is never late - there is nothing to be late against - and one
    that has arrived cannot become late afterwards however long it took.
    """
    if status not in IN_FLIGHT_STATUSES or expected_delivery_date is None:
        return False
    return expected_delivery_date < (as_of or today())


def delay_days(expected_delivery_date: date | None, as_of: date | None = None) -> int:
    """How many days a promise is overdue. Never negative."""
    moment = as_of or today()
    if expected_delivery_date is None or expected_delivery_date >= moment:
        return 0
    return (moment - expected_delivery_date).days


def arrived_late(*, expected_delivery_date: date | None, actual_delivery_date: date | None) -> bool:
    """Did the consignment arrive after the date we promised?

    Distinct from :func:`is_delayed`, which is about a consignment still on the
    road. This one is asked at confirmation time and decides whether a delay
    reason is mandatory: an on-time-delivery report with unexplained late rows
    tells nobody anything.
    """
    if expected_delivery_date is None or actual_delivery_date is None:
        return False
    return actual_delivery_date > expected_delivery_date


def assert_delay_explained(
    *,
    code: str,
    expected_delivery_date: date | None,
    actual_delivery_date: date,
    reason: str | None,
) -> bool:
    """Refuse a late arrival that carries no delay reason; answer whether it was late.

    One rule for every path that can mark a consignment DELIVERED - the
    confirmation form and a tracking event alike. The second path used to skip
    it, so a courier feed or the timeline dialog could land a late consignment
    with nothing recorded against the delay, and the on-time-delivery report
    grew rows nobody could explain.
    """
    late = arrived_late(
        expected_delivery_date=expected_delivery_date, actual_delivery_date=actual_delivery_date
    )
    if late and not reason:
        raise BusinessRuleError(
            f"{code} was promised for {expected_delivery_date} and arrived on "
            f"{actual_delivery_date}; a delay reason is required.",
            details={
                "expected_delivery_date": str(expected_delivery_date),
                "actual_delivery_date": str(actual_delivery_date),
                "delay_days": delay_days(expected_delivery_date, actual_delivery_date),
            },
        )
    return late


def qc_is_pending(status: DispatchStatus) -> bool:
    """Does the receiver still owe an inspection on this consignment?"""
    return status is DispatchStatus.QC_PENDING


def grn_is_pending(status: DispatchStatus) -> bool:
    """Has the consignment arrived without being booked in at the far end?"""
    return status in GRN_OUTSTANDING_STATUSES


def build_line_movements(
    line: DispatchLine,
    *,
    item: DispatchItem,
    dispatch_code: str,
    dispatch_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    occurred_at: datetime,
) -> list[MovementRequest]:
    """The ledger rows one unreserved dispatch line has to post.

    One movement for an ordinary line; one movement *per serial* for a
    serial-tracked one, because the inventory engine requires a serial on every
    movement of such an item and refuses any quantity but one against it. Pure,
    so the expansion is testable without a database.
    """
    reason = f"Dispatch {dispatch_code} line {line.line_number}"[:255]
    common: dict[str, Any] = {
        "item_id": line.item_id,
        "warehouse_id": warehouse_id,
        "transaction_type": TransactionType.DISPATCH,
        "reference_type": MOVEMENT_REFERENCE_DISPATCH,
        "reference_id": dispatch_id,
        "reference_code": dispatch_code,
        "reason": reason,
        "batch_id": line.batch_id,
        "stock_state": StockState.AVAILABLE,
        "occurred_at": occurred_at,
    }

    if item.is_serial_tracked:
        return [
            MovementRequest(quantity=Decimal("1"), serial_id=serial_id, **common)
            for serial_id in line.serial_ids
        ]
    return [MovementRequest(quantity=to_quantity(line.quantity), **common)]


# -----------------------------------------------------------------------------
# Presentation helpers
# -----------------------------------------------------------------------------


def dispatch_detail(dispatch: Dispatch, lines: Sequence[DispatchLine]) -> DispatchDetail:
    detail = DispatchDetail.model_validate(dispatch)
    detail.lines = [DispatchLineRead.model_validate(line) for line in lines]
    return detail


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


async def _lock_dispatch(repo: DispatchRepository, dispatch_id: uuid.UUID) -> Dispatch:
    """Take the header's row lock.

    Must be called *inside* a :func:`~src.core.database.transaction` block. A
    ``SELECT ... FOR UPDATE`` issued outside one is held only by the implicit
    transaction SQLAlchemy opens for the statement, which nothing in the request
    lifecycle then commits - so the lock would be released at an unpredictable
    point and the read-modify-write it exists to protect would not be protected
    at all (database.md section 4.4).
    """
    dispatch = await repo.lock_for_update(dispatch_id)
    if dispatch is None:
        raise NotFoundError("Dispatch", dispatch_id)
    return dispatch


async def _transition(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch: Dispatch,
    target: DispatchStatus,
    *,
    reason: str,
    repo: DispatchRepository,
    action: AuditAction = AuditAction.DISPATCH_CHANGE,
) -> Dispatch:
    """Move a consignment's status and record it.

    Every status change funnels through here, so there is exactly one place that
    writes ``dispatches.status`` and exactly one place that writes the matching
    audit row (master rules section 73).
    """
    current = DispatchStatus(dispatch.status)
    DISPATCH_LIFECYCLE.assert_transition(current, target)
    dispatch.status = target
    repo.touch(dispatch)
    await session.flush()

    await record_status_change(
        session,
        company_id=tenant.company_id,
        entity_type=AUDIT_DISPATCH,
        entity_id=dispatch.id,
        entity_code=dispatch.code,
        from_status=str(current),
        to_status=str(target),
        user_id=tenant.user_id,
        action=action,
        reason=reason,
    )
    return dispatch


def _assert_editable(dispatch: Dispatch) -> None:
    if DispatchStatus(dispatch.status) not in EDITABLE_STATUSES:
        raise BusinessRuleError(
            f"{dispatch.code} is {dispatch.status}; a consignment can only be changed before "
            "it leaves.",
            details={"dispatch_id": str(dispatch.id), "status": str(dispatch.status)},
        )


async def _record_event(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch: Dispatch,
    *,
    event_status: DispatchStatus,
    event_time: datetime | None,
    location: str | None,
    remark: str | None,
    source: TrackingEventSource,
) -> DispatchTrackingEvent:
    """Append one tracking event. Never updates an existing one.

    ``recorded_by_id`` is left null for a machine source: attributing a courier's
    scan to whichever user happened to trigger the import would put a person's
    name against a fact they never asserted.
    """
    event = DispatchTrackingEvent(
        company_id=tenant.company_id,
        dispatch_id=dispatch.id,
        event_status=event_status,
        event_time=event_time or utcnow(),
        location=location,
        remark=remark,
        recorded_by_id=tenant.user_id if source is TrackingEventSource.MANUAL else None,
        source=source,
        created_by_id=tenant.user_id,
    )
    session.add(event)
    await session.flush()
    return event


def _assert_unique_sales_order_lines(lines: Sequence[SalesOrderLineReference]) -> None:
    duplicates = duplicate_sales_order_line_ids(lines)
    if duplicates:
        raise BusinessRuleError(
            "The same sales-order line cannot appear twice on a consignment; "
            "combine its quantities.",
            details={"sales_order_line_ids": [str(line_id) for line_id in duplicates]},
        )


async def _validate_lines(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    lines: Sequence[DispatchLineIn],
    warehouse_id: uuid.UUID,
    sales_order_id: uuid.UUID | None,
    packing_order_id: uuid.UUID | None,
) -> dict[uuid.UUID, DispatchItem]:
    """Check a whole line set in five batched statements, not five per line.

    Everything that could be asked line by line - items, batches, serials,
    cartons, order lines - is asked for the whole set at once, because a
    twenty-line consignment validated with a lookup per line would be a hundred
    round trips on the write path (section 32).
    """
    if len(lines) > MAX_LINES_PER_DISPATCH:
        raise BusinessRuleError(
            f"A consignment may carry at most {MAX_LINES_PER_DISPATCH} lines.",
            details={"line_count": len(lines)},
        )

    _assert_unique_sales_order_lines(lines)

    items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])
    batches = await BatchLookupRepository(session, tenant).load(
        [line.batch_id for line in lines if line.batch_id is not None]
    )
    serial_ids = [serial for line in lines for serial in line.serial_ids]
    serials = await SerialLookupRepository(session, tenant).load(serial_ids)

    if len(set(serial_ids)) != len(serial_ids):
        raise BusinessRuleError(
            "The same serial number appears on two lines of this consignment.",
            details={"serial_count": len(serial_ids)},
        )

    order_lines: dict[uuid.UUID, SalesOrderLine] = {}
    referenced = [line.sales_order_line_id for line in lines if line.sales_order_line_id]
    if referenced:
        if sales_order_id is None:
            raise BusinessRuleError(
                "A line cannot reference a sales-order line when the consignment names no "
                "sales order."
            )
        found = await SalesOrderLineRepository(session, tenant).get_many(referenced)
        order_lines = {row.id: row for row in found}

    carton_ids = [carton for line in lines for carton in line.carton_ids]
    sealed_cartons: set[uuid.UUID] = set()
    if carton_ids:
        if packing_order_id is None:
            raise BusinessRuleError(
                "Cartons can only be loaded onto a consignment that names the packing order "
                "they came from."
            )
        sealed_cartons = await CartonLookupRepository(session, tenant).sealed_cartons_of(
            packing_order_id, carton_ids
        )

    for position, line in enumerate(lines, start=1):
        label = f"Line {position}"
        item = items.get(line.item_id)
        if item is None:
            raise NotFoundError("Item", line.item_id)
        # A deactivated item may still give stock up - stranding existing stock
        # behind a flag is worse than the thing the flag prevents - so an
        # inactive item is not refused here. The inventory engine applies the
        # same rule to the movement itself.

        if item.is_batch_tracked and line.batch_id is None:
            raise BusinessRuleError(
                f"{label}: {item.code} is batch tracked, so the batch being shipped must be named.",
                code=ErrorCode.BATCH_REQUIRED,
                details={"item_code": item.code},
            )
        if line.batch_id is not None:
            batch = batches.get(line.batch_id)
            if batch is None:
                raise NotFoundError("Stock batch", line.batch_id)
            if batch.item_id != line.item_id:
                raise BusinessRuleError(
                    f"{label}: batch {batch.batch_number} belongs to a different item.",
                    details={"batch_id": str(line.batch_id), "item_code": item.code},
                )

        if item.is_serial_tracked:
            if not line.serial_ids:
                raise BusinessRuleError(
                    f"{label}: {item.code} is serial tracked, so every unit shipped must be "
                    "identified.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={"item_code": item.code},
                )
        elif line.serial_ids:
            raise BusinessRuleError(
                f"{label}: {item.code} is not serial tracked and cannot carry serial numbers.",
                details={"item_code": item.code},
            )

        for serial_id in line.serial_ids:
            serial = serials.get(serial_id)
            if serial is None:
                raise NotFoundError("Stock serial", serial_id)
            if serial.item_id != line.item_id:
                raise BusinessRuleError(
                    f"{label}: serial {serial.serial_number} belongs to a different item.",
                    details={"serial_id": str(serial_id), "item_code": item.code},
                )
            if serial.status is not SerialStatus.IN_STOCK:
                raise BusinessRuleError(
                    f"{label}: serial {serial.serial_number} is {serial.status} and is not in "
                    "stock to be shipped.",
                    details={"serial_id": str(serial_id), "status": str(serial.status)},
                )
            if serial.warehouse_id is not None and serial.warehouse_id != warehouse_id:
                raise BusinessRuleError(
                    f"{label}: serial {serial.serial_number} is held at a different warehouse.",
                    details={"serial_id": str(serial_id)},
                )

        missing_cartons = set(line.carton_ids) - sealed_cartons
        if missing_cartons:
            raise BusinessRuleError(
                f"{label}: {len(missing_cartons)} of the cartons named are not sealed cartons "
                "of this consignment's packing order.",
                details={"carton_ids": sorted(str(c) for c in missing_cartons)},
            )

        if line.sales_order_line_id is not None:
            order_line = order_lines.get(line.sales_order_line_id)
            if order_line is None:
                raise NotFoundError("Sales order line", line.sales_order_line_id)
            if order_line.sales_order_id != sales_order_id:
                raise BusinessRuleError(
                    f"{label}: that order line belongs to a different sales order.",
                    details={"sales_order_line_id": str(line.sales_order_line_id)},
                )
            if order_line.item_id != line.item_id:
                raise BusinessRuleError(
                    f"{label}: order line {order_line.line_number} is for a different item.",
                    details={"sales_order_line_id": str(line.sales_order_line_id)},
                )
            # Checked here so a plan that cannot possibly ship is refused while
            # it is still a plan. It is checked *again* under a row lock when the
            # consignment actually leaves - that one is the authority, because
            # another consignment may take the balance in between.
            dispatch_balance(
                ordered=Decimal(order_line.quantity),
                dispatched_before=Decimal(order_line.dispatched_quantity),
                dispatching_now=line.quantity,
                line_label=f"{label} ({item.code})",
            )

    return items


async def _persist_lines(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch: Dispatch,
    payload_lines: Sequence[DispatchLineIn],
) -> list[DispatchLine]:
    repo = DispatchLineRepository(session, tenant)
    created = [
        repo.add(
            DispatchLine(
                dispatch_id=dispatch.id,
                line_number=number,
                sales_order_line_id=line.sales_order_line_id,
                item_id=line.item_id,
                quantity=to_quantity(line.quantity),
                batch_id=line.batch_id,
                serial_ids=list(line.serial_ids),
                carton_ids=list(line.carton_ids),
                notes=line.notes,
            )
        )
        for number, line in enumerate(payload_lines, start=1)
    ]
    await session.flush()
    return created


async def _validate_header(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    dispatch_type: DispatchType,
    customer_id: uuid.UUID | None,
    vendor_id: uuid.UUID | None,
    warehouse_id: uuid.UUID,
    transporter_id: uuid.UUID | None,
    invoice_document_id: uuid.UUID | None,
    sales_order_id: uuid.UUID | None,
) -> None:
    """Every master-data reference on the header, checked before anything is written."""
    if customer_id is not None:
        customer = await CustomerLookupRepository(session, tenant).get_facts(customer_id)
        if customer is None:
            raise NotFoundError("Customer", customer_id)
        if not customer.is_active:
            raise BusinessRuleError(
                f"Customer {customer.code} is inactive.",
                details={"customer_id": str(customer_id)},
            )
        if customer.is_blocked:
            raise BusinessRuleError(
                f"Customer {customer.code} is blocked; goods may not be dispatched to them.",
                details={"customer_id": str(customer_id)},
            )

    if vendor_id is not None:
        vendor = await VendorLookupRepository(session, tenant).get_facts(vendor_id)
        if vendor is None:
            raise NotFoundError("Vendor", vendor_id)
        if not vendor.is_active:
            raise BusinessRuleError(
                f"Vendor {vendor.code} is inactive.", details={"vendor_id": str(vendor_id)}
            )
        if vendor.is_blocked:
            raise BusinessRuleError(
                f"Vendor {vendor.code} is not approved; goods may not be sent to them.",
                details={"vendor_id": str(vendor_id)},
            )

    warehouse = await WarehouseLookupRepository(session, tenant).get_facts(warehouse_id)
    if warehouse is None:
        raise NotFoundError("Warehouse", warehouse_id)
    if not warehouse[1]:
        # A deactivated warehouse may still give stock up (the inventory engine
        # allows outbound movements from one), so this is not a refusal to ship -
        # it is a refusal to *plan* new work into a store being wound down.
        raise BusinessRuleError(
            f"Warehouse {warehouse[0]} is inactive and cannot be dispatched from.",
            details={"warehouse_id": str(warehouse_id)},
        )

    if transporter_id is not None:
        transporter = await TransporterLookupRepository(session, tenant).get_facts(transporter_id)
        if transporter is None:
            raise NotFoundError("Transporter", transporter_id)
        if not transporter[2]:
            raise BusinessRuleError(
                f"Transporter {transporter[0]} is inactive.",
                details={"transporter_id": str(transporter_id)},
            )

    if invoice_document_id is not None and not await DocumentLookupRepository(
        session, tenant
    ).exists_in_tenant(invoice_document_id):
        raise NotFoundError("Document", invoice_document_id)

    if sales_order_id is not None:
        header = await SalesOrderRepository(session, tenant).get_code(sales_order_id)
        if header is None:
            raise NotFoundError("Sales order", sales_order_id)
        if customer_id is not None and header[1] != customer_id:
            # Loading one customer's order onto another's lorry is how goods
            # reach the wrong address, and no later check catches it.
            raise BusinessRuleError(
                f"Sales order {header[0]} belongs to a different customer.",
                details={"sales_order_id": str(sales_order_id)},
            )
        if dispatch_type is not DispatchType.SALES:
            raise BusinessRuleError(
                "A sales order can only be dispatched by a SALES consignment.",
                details={"dispatch_type": str(dispatch_type)},
            )


async def _claim_packing_order(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    dispatch: Dispatch,
    packing_order_id: uuid.UUID,
) -> None:
    """Take the boxes off the ready-for-dispatch feed and onto this consignment.

    Done when the consignment is raised rather than when it leaves: the feed
    exists so two dispatchers cannot both plan around the same boxes, and a claim
    that waited until loading would not prevent that.
    """
    facts = await PackingOrderLookupRepository(session, tenant).get_facts(packing_order_id)
    if facts is None:
        raise NotFoundError("Packing order", packing_order_id)
    code, status, customer_id, order_sales_order_id, claimed_by = facts

    if claimed_by is not None and claimed_by != dispatch.id:
        raise BusinessRuleError(
            f"Packing order {code} has already been taken by another consignment.",
            code=ErrorCode.CONFLICT,
            details={"packing_order_id": str(packing_order_id)},
        )
    if status is not PackingOrderStatus.PACKED and claimed_by is None:
        raise BusinessRuleError(
            f"Packing order {code} is {status}; only a PACKED order can be loaded.",
            details={"packing_order_id": str(packing_order_id), "status": str(status)},
        )
    if dispatch.customer_id is not None and customer_id != dispatch.customer_id:
        raise BusinessRuleError(
            f"Packing order {code} is addressed to a different customer.",
            details={"packing_order_id": str(packing_order_id)},
        )
    if (
        dispatch.sales_order_id is not None
        and order_sales_order_id is not None
        and order_sales_order_id != dispatch.sales_order_id
    ):
        raise BusinessRuleError(
            f"Packing order {code} was packed against a different sales order.",
            details={"packing_order_id": str(packing_order_id)},
        )

    await packing.hand_over_to_dispatch(
        session,
        tenant,
        packing_order_id,
        dispatch_id=dispatch.id,
        reason=f"Loaded onto dispatch {dispatch.code}",
    )


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


async def list_dispatches(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: DispatchFilters,
    params: PageParams,
    sort: SortParams,
) -> tuple[list[DispatchSummary], OffsetPagination]:
    repo = DispatchRepository(session, tenant)
    rows, pagination = await repo.list_page_filtered(filters, params, sort, as_of=today())
    return [DispatchSummary.model_validate(row) for row in rows], pagination


async def get_dispatch_detail(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID
) -> DispatchDetail:
    repo = DispatchRepository(session, tenant)
    dispatch = await repo.get_or_404(dispatch_id)
    lines = await DispatchLineRepository(session, tenant).for_dispatch(dispatch.id)
    return dispatch_detail(dispatch, lines)


async def tracking_board(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    filters: DispatchFilters,
    params: PageParams,
) -> tuple[list[TrackingBoardRow], OffsetPagination]:
    """The admin tracking board (master rules section 23).

    One query. Every column the board prints - counterparty, order reference,
    carrier, tracking id, status, promised date, delay flag, QC and GRN pending -
    comes back from a single joined statement rather than from a list plus a
    lookup per row.
    """
    repo = DispatchRepository(session, tenant)
    return await repo.tracking_board_page(filters, params, as_of=today())


async def list_delayed(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    params: PageParams,
    warehouse_id: uuid.UUID | None = None,
) -> tuple[list[DelayedDispatchRow], OffsetPagination]:
    """Consignments past their promised date with no delivery recorded."""
    repo = DispatchRepository(session, tenant)
    return await repo.delayed_page(params, as_of=today(), warehouse_id=warehouse_id)


async def list_tracking_events(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch_id: uuid.UUID,
    *,
    params: CursorParams,
) -> tuple[list[TrackingEventRead], CursorPagination]:
    repo = DispatchRepository(session, tenant)
    await repo.get_or_404(dispatch_id)
    events, pagination = await DispatchTrackingEventRepository(session, tenant).for_dispatch(
        dispatch_id, params=params
    )
    return [TrackingEventRead.model_validate(event) for event in events], pagination


# -----------------------------------------------------------------------------
# Writes
# -----------------------------------------------------------------------------


async def create_dispatch(
    session: AsyncSession, tenant: TenantContext, payload: DispatchCreate
) -> DispatchDetail:
    """Raise a consignment in ``NOT_DISPATCHED``.

    Nothing moves: this is the loading plan. Naming a packing order does claim
    its boxes, which is the one side effect - see :func:`_claim_packing_order`.
    """
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        await _validate_header(
            session,
            tenant,
            dispatch_type=payload.dispatch_type,
            customer_id=payload.customer_id,
            vendor_id=payload.vendor_id,
            warehouse_id=payload.warehouse_id,
            transporter_id=payload.transporter_id,
            invoice_document_id=payload.invoice_document_id,
            sales_order_id=payload.sales_order_id,
        )
        await _validate_lines(
            session,
            tenant,
            lines=payload.lines,
            warehouse_id=payload.warehouse_id,
            sales_order_id=payload.sales_order_id,
            packing_order_id=payload.packing_order_id,
        )

        code = await next_document_number(session, tenant.company_id, DocTypes.DISPATCH)
        dispatch = repo.add(
            Dispatch(
                code=code,
                dispatch_type=payload.dispatch_type,
                sales_order_id=payload.sales_order_id,
                packing_order_id=payload.packing_order_id,
                customer_id=payload.customer_id,
                vendor_id=payload.vendor_id,
                warehouse_id=payload.warehouse_id,
                status=DispatchStatus.NOT_DISPATCHED,
                dispatch_date=payload.dispatch_date,
                expected_delivery_date=payload.expected_delivery_date,
                transporter_id=payload.transporter_id,
                courier_name=payload.courier_name,
                transport_name=payload.transport_name,
                tracking_id=payload.tracking_id,
                lr_number=payload.lr_number,
                vehicle_number=payload.vehicle_number,
                driver_name=payload.driver_name,
                driver_mobile=payload.driver_mobile,
                total_boxes=payload.total_boxes or 0,
                total_weight_kg=to_quantity(payload.total_weight_kg or ZERO),
                freight_amount=payload.freight_amount or ZERO,
                freight_paid_by=payload.freight_paid_by,
                eway_bill_number=payload.eway_bill_number,
                invoice_document_id=payload.invoice_document_id,
                notes=payload.notes,
            )
        )
        await session.flush()

        lines = await _persist_lines(session, tenant, dispatch, payload.lines)

        if payload.packing_order_id is not None:
            await _claim_packing_order(
                session, tenant, dispatch=dispatch, packing_order_id=payload.packing_order_id
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.CREATE,
            entity_type=AUDIT_DISPATCH,
            entity_id=dispatch.id,
            entity_code=dispatch.code,
            summary=f"Raised dispatch {dispatch.code} with {len(lines)} line(s)",
            user_id=tenant.user_id,
            new_values=snapshot(dispatch, DISPATCH_AUDITED_FIELDS),
            context={"line_count": len(lines)},
        )

    return dispatch_detail(dispatch, lines)


async def update_dispatch(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, payload: DispatchUpdate
) -> DispatchDetail:
    """Amend a consignment that has not left.

    Fields absent from the payload are untouched; an explicit ``null`` clears
    only the transport fields it is meaningful to clear. That distinction matters
    because a PATCH body cannot otherwise say "leave the tracking id alone"
    differently from "there is no tracking id".
    """
    repo = DispatchRepository(session, tenant)
    line_repo = DispatchLineRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        _assert_editable(dispatch)
        before = snapshot(dispatch, DISPATCH_AUDITED_FIELDS)

        sent = payload.model_dump(exclude_unset=True)
        line_payload = sent.pop("lines", None)

        # Read before the payload is applied: afterwards the column already holds
        # the new value and the comparison below could not tell a first claim
        # from a swap.
        held_packing_order = dispatch.packing_order_id
        if (
            payload.packing_order_id is not None
            and held_packing_order is not None
            and held_packing_order != payload.packing_order_id
        ):
            # Swapping the boxes would leave the first packing order claimed by a
            # consignment that no longer carries it. Releasing it needs a reverse
            # handover the packing contract does not offer, so the honest answer
            # is to refuse rather than to leave a dangling claim.
            raise BusinessRuleError(
                f"{dispatch.code} already carries a different packing order; raise a new "
                "consignment for other boxes.",
                details={"packing_order_id": str(held_packing_order)},
            )

        for field, value in sent.items():
            if value is None and field not in CLEARABLE_UPDATE_FIELDS:
                continue
            setattr(dispatch, field, value)

        await _validate_header(
            session,
            tenant,
            dispatch_type=DispatchType(dispatch.dispatch_type),
            customer_id=dispatch.customer_id,
            vendor_id=dispatch.vendor_id,
            warehouse_id=dispatch.warehouse_id,
            transporter_id=dispatch.transporter_id,
            invoice_document_id=dispatch.invoice_document_id,
            sales_order_id=dispatch.sales_order_id,
        )

        if line_payload is not None:
            await _validate_lines(
                session,
                tenant,
                lines=payload.lines or [],
                warehouse_id=dispatch.warehouse_id,
                sales_order_id=dispatch.sales_order_id,
                packing_order_id=dispatch.packing_order_id,
            )
            # Safe only because nothing has been posted: past DISPATCHED a line
            # carries the ledger row it wrote, and deleting one would orphan a
            # movement. ``_assert_editable`` above is what guarantees that.
            await line_repo.delete_for_dispatch(dispatch.id)
            lines = await _persist_lines(session, tenant, dispatch, payload.lines or [])
        else:
            lines = await line_repo.for_dispatch(dispatch.id)

        if payload.packing_order_id is not None and dispatch.packing_order_id is not None:
            await _claim_packing_order(
                session, tenant, dispatch=dispatch, packing_order_id=dispatch.packing_order_id
            )

        repo.touch(dispatch)
        await session.flush()

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_DISPATCH,
            entity_id=dispatch.id,
            entity_code=dispatch.code,
            summary=f"Updated dispatch {dispatch.code}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(dispatch, DISPATCH_AUDITED_FIELDS),
            context={"lines_replaced": line_payload is not None},
        )

    return dispatch_detail(dispatch, lines)


async def delete_draft_dispatch(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, *, reason: str
) -> None:
    """Remove a consignment that was raised and never used.

    Permitted only in ``NOT_DISPATCHED`` and only while it holds no packing
    order: a claimed packing order would be left marked HANDED_OVER to a
    consignment that no longer exists, which is worse than a cancelled document.
    Anything further along is cancelled instead, so the trail survives.
    """
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        if DispatchStatus(dispatch.status) is not DispatchStatus.NOT_DISPATCHED:
            raise BusinessRuleError(
                f"{dispatch.code} is {dispatch.status}; only a consignment that has not been "
                "prepared can be deleted. Cancel it instead.",
                details={"status": str(dispatch.status)},
            )
        if dispatch.packing_order_id is not None:
            raise BusinessRuleError(
                f"{dispatch.code} holds a packing order and cannot be deleted; cancel it "
                "instead so the claim on those boxes is recorded.",
                details={"packing_order_id": str(dispatch.packing_order_id)},
            )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DELETE,
            entity_type=AUDIT_DISPATCH,
            entity_id=dispatch.id,
            entity_code=dispatch.code,
            summary=f"Deleted unused dispatch {dispatch.code}",
            user_id=tenant.user_id,
            old_values=snapshot(dispatch, DISPATCH_AUDITED_FIELDS),
            context={"reason": reason},
        )
        await DispatchLineRepository(session, tenant).delete_for_dispatch(dispatch.id)
        await repo.hard_delete(dispatch)


async def mark_ready(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, *, reason: str
) -> DispatchDetail:
    """The goods are loaded and the paperwork is done; the lorry can go."""
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        lines = await DispatchLineRepository(session, tenant).for_dispatch(dispatch.id)
        if not lines:
            raise BusinessRuleError(
                f"{dispatch.code} has no lines; there is nothing to dispatch.",
                details={"dispatch_id": str(dispatch.id)},
            )

        await _transition(
            session, tenant, dispatch, DispatchStatus.READY_FOR_DISPATCH, reason=reason, repo=repo
        )
        await _record_event(
            session,
            tenant,
            dispatch,
            event_status=DispatchStatus.READY_FOR_DISPATCH,
            event_time=None,
            location=None,
            remark=reason,
            source=TrackingEventSource.MANUAL,
        )

    return dispatch_detail(dispatch, lines)


async def cancel_dispatch(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, *, reason: str
) -> DispatchDetail:
    """Call off a consignment before it leaves.

    Refused once anything has shipped: the ledger records goods that left, and a
    document claiming never to have existed would contradict it.
    """
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        await _transition(
            session,
            tenant,
            dispatch,
            DispatchStatus.CANCELLED,
            reason=reason,
            repo=repo,
            action=AuditAction.CANCEL,
        )
        await _record_event(
            session,
            tenant,
            dispatch,
            event_status=DispatchStatus.CANCELLED,
            event_time=None,
            location=None,
            remark=reason,
            source=TrackingEventSource.MANUAL,
        )
        lines = await DispatchLineRepository(session, tenant).for_dispatch(dispatch.id)

    return dispatch_detail(dispatch, lines)


async def dispatch_consignment(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, payload: DispatchRequest
) -> DispatchResult:
    """Send the consignment. The only function here that moves stock.

    In one transaction: lock the consignment and every sales-order line it draws
    on, re-check the partial-dispatch arithmetic against the freshly-locked
    quantities, consume the reservations, post the movements for anything not
    reserved, advance the order lines and the order, move the status and write
    the audit rows. Any failure rolls all of it back - there is no state in which
    stock has moved and the order does not know.
    """
    repo = DispatchRepository(session, tenant)
    line_repo = DispatchLineRepository(session, tenant)
    order_repo = SalesOrderRepository(session, tenant)
    order_line_repo = SalesOrderLineRepository(session, tenant)

    consumed: list[uuid.UUID] = []

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        # Asserted up front so an illegal transition costs one statement rather
        # than a full validation pass followed by a rollback.
        DISPATCH_LIFECYCLE.assert_transition(
            DispatchStatus(dispatch.status), DispatchStatus.DISPATCHED
        )

        lines = await line_repo.for_dispatch(dispatch.id)
        if not lines:
            raise BusinessRuleError(
                f"{dispatch.code} has no lines; there is nothing to dispatch.",
                details={"dispatch_id": str(dispatch.id)},
            )

        # Old drafts may predate request validation. Refuse before consuming any
        # reservation or posting stock, while the consignment is locked.
        _assert_unique_sales_order_lines(lines)
        items = await ItemLookupRepository(session, tenant).load([line.item_id for line in lines])

        # Lock the order lines this consignment draws on. Ordered by primary key
        # inside the repository so two consignments over an overlapping set queue
        # instead of deadlocking (database.md section 4.4).
        order_line_ids = [
            line.sales_order_line_id for line in lines if line.sales_order_line_id is not None
        ]
        order_lines = {row.id: row for row in await order_line_repo.lock_lines(order_line_ids)}
        sales_order = None
        if dispatch.sales_order_id is not None:
            sales_order = await order_repo.lock_header(dispatch.sales_order_id)
            if sales_order is None:
                raise NotFoundError("Sales order", dispatch.sales_order_id)
            # Checked here, under the lock and before any reservation is consumed
            # or movement posted, so an unallocated order is refused with the
            # remedy named rather than by the status walk at the very end.
            assert_order_dispatchable(sales_order.code, SalesOrderStatus(sales_order.status))

        # Re-check the arithmetic against what the lock just revealed. The value
        # read at planning time is worthless: another consignment may have taken
        # the balance in between (database.md section 4.4, step 3).
        balances: dict[uuid.UUID, LineBalance] = {}
        for line in lines:
            if line.sales_order_line_id is None:
                continue
            order_line = order_lines.get(line.sales_order_line_id)
            if order_line is None:
                raise NotFoundError("Sales order line", line.sales_order_line_id)
            item = items.get(line.item_id)
            balances[line.id] = dispatch_balance(
                ordered=Decimal(order_line.quantity),
                dispatched_before=Decimal(order_line.dispatched_quantity),
                dispatching_now=Decimal(line.quantity),
                line_label=f"Line {line.line_number} ({item.code if item else line.item_id})",
            )

        occurred_at = utcnow()
        if payload.dispatch_date is not None:
            dispatch.dispatch_date = payload.dispatch_date
        if payload.expected_delivery_date is not None:
            dispatch.expected_delivery_date = payload.expected_delivery_date
        if payload.lr_number is not None:
            dispatch.lr_number = payload.lr_number
        if payload.vehicle_number is not None:
            dispatch.vehicle_number = payload.vehicle_number
        if payload.tracking_id is not None:
            dispatch.tracking_id = payload.tracking_id

        # Deterministic order over the whole line set, used for both halves of
        # the posting. The balance rows a consignment touches are keyed on
        # (item, batch), so taking them in that order gives two consignments
        # sharing items a consistent sequence; the engine's own primary-key
        # ordering handles the rest, and ``lock_timeout`` turns whatever is left
        # into a retryable StockLockTimeoutError rather than a stuck worker.
        ordered_lines = sorted(
            lines, key=lambda line: (str(line.item_id), str(line.batch_id or ""), str(line.id))
        )

        direct_movements: list[MovementRequest] = []
        movement_owner: list[DispatchLine] = []

        for line in ordered_lines:
            item = items.get(line.item_id)
            if item is None:
                raise NotFoundError("Item", line.item_id)

            order_line = (
                order_lines.get(line.sales_order_line_id)
                if line.sales_order_line_id is not None
                else None
            )
            reservation_id = order_line.reservation_id if order_line is not None else None

            if reservation_id is not None:
                if item.is_serial_tracked:
                    raise BusinessRuleError(
                        SERIAL_RESERVATION_MESSAGE.format(item_code=item.code),
                        code=ErrorCode.SERIAL_REQUIRED,
                        details={
                            "item_code": item.code,
                            "dispatch_line_id": str(line.id),
                            "reservation_id": str(reservation_id),
                        },
                    )
                # Consumes the promise and posts the DISPATCH movement in one
                # step, in the right order - see allocation.consume's docstring.
                # A call per reservation is unavoidable: each is a separate row
                # that has to be locked and drawn down individually, and the set
                # is bounded by MAX_LINES_PER_DISPATCH.
                reservation = await allocation.consume(
                    session,
                    tenant,
                    reservation_id=reservation_id,
                    quantity=to_quantity(line.quantity),
                    reference_type=MOVEMENT_REFERENCE_DISPATCH,
                    reference_id=dispatch.id,
                    reference_code=dispatch.code,
                    transaction_type=TransactionType.DISPATCH,
                    occurred_at=occurred_at,
                )
                line.reservation_id = reservation.id
                consumed.append(reservation.id)
                continue

            movements = build_line_movements(
                line,
                item=item,
                dispatch_code=dispatch.code,
                dispatch_id=dispatch.id,
                warehouse_id=dispatch.warehouse_id,
                occurred_at=occurred_at,
            )
            direct_movements.extend(movements)
            movement_owner.extend([line] * len(movements))

        if direct_movements:
            posted = await post_movements(session, tenant, direct_movements)
            for line, transaction_row in zip(movement_owner, posted, strict=True):
                # A serial-tracked line posts several rows; the first is stored
                # as the line's anchor and the whole set stays reachable through
                # the (reference_type, reference_id) index on the ledger.
                if line.inventory_transaction_id is None:
                    line.inventory_transaction_id = transaction_row.id

        # --- fulfilment arithmetic on the order ------------------------------
        for line in lines:
            if line.sales_order_line_id is None:
                continue
            order_line = order_lines[line.sales_order_line_id]
            balance = balances[line.id]
            order_line.dispatched_quantity = balance.dispatched_after
            order_line.status = line_status_for(
                quantity=Decimal(order_line.quantity),
                allocated_quantity=Decimal(order_line.allocated_quantity),
                dispatched_quantity=balance.dispatched_after,
            )
            order_line_repo.touch(order_line)

        dispatch.dispatched_at = occurred_at
        await session.flush()

        open_lines = 0
        order_status: str | None = None
        if sales_order is not None:
            open_lines = await order_line_repo.open_line_count(sales_order.id)
            order_status = await _advance_sales_order(
                session,
                tenant,
                sales_order=sales_order,
                fully_dispatched=open_lines == 0,
                dispatch_code=dispatch.code,
            )

        await _transition(
            session,
            tenant,
            dispatch,
            DispatchStatus.DISPATCHED,
            reason=payload.reason,
            repo=repo,
        )
        await _record_event(
            session,
            tenant,
            dispatch,
            event_status=DispatchStatus.DISPATCHED,
            event_time=occurred_at,
            location=payload.location,
            remark=payload.reason,
            source=TrackingEventSource.MANUAL,
        )

        posted_ids = await LedgerLookupRepository(session, tenant).posted_for(
            MOVEMENT_REFERENCE_DISPATCH, dispatch.id
        )

        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_DISPATCH,
            entity_id=dispatch.id,
            entity_code=dispatch.code,
            summary=(
                f"Dispatched {dispatch.code}: {len(lines)} line(s), "
                f"{len(posted_ids)} stock movement(s)"
            ),
            user_id=tenant.user_id,
            context={
                "line_count": len(lines),
                "consumed_reservation_ids": [str(r) for r in consumed],
                "inventory_transaction_ids": [str(t) for t in posted_ids],
                "sales_order_id": str(dispatch.sales_order_id) if dispatch.sales_order_id else None,
                "sales_order_status": order_status,
                "open_sales_order_lines": open_lines,
                "reason": payload.reason,
            },
        )

    return DispatchResult(
        dispatch=dispatch_detail(dispatch, lines),
        inventory_transaction_ids=posted_ids,
        consumed_reservation_ids=consumed,
        sales_order_id=dispatch.sales_order_id,
        sales_order_status=order_status,
        open_sales_order_lines=open_lines,
    )


async def _advance_sales_order(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    sales_order: Any,
    fully_dispatched: bool,
    dispatch_code: str,
) -> str:
    """Walk the order to the status its dispatched quantities now imply.

    Each hop is asserted by the sales module's own state machine and audited
    separately, so the order's history reads as the sequence it actually went
    through rather than as a jump the machine does not describe.
    """
    current = SalesOrderStatus(sales_order.status)
    for target in sales_order_status_path(current, fully_dispatched=fully_dispatched):
        SALES_ORDER_LIFECYCLE.assert_transition(current, target)
        sales_order.status = target
        sales_order.updated_by_id = tenant.user_id
        await session.flush()
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type=AUDIT_SALES_ORDER,
            entity_id=sales_order.id,
            entity_code=sales_order.code,
            from_status=str(current),
            to_status=str(target),
            user_id=tenant.user_id,
            action=AuditAction.DISPATCH_CHANGE,
            reason=f"Dispatch {dispatch_code}",
        )
        current = target
    return str(current)


async def mark_in_transit(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, payload: InTransitRequest
) -> DispatchDetail:
    """Report that the consignment is moving, with where it was last seen."""
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        await _transition(
            session, tenant, dispatch, DispatchStatus.IN_TRANSIT, reason=payload.reason, repo=repo
        )
        await _record_event(
            session,
            tenant,
            dispatch,
            event_status=DispatchStatus.IN_TRANSIT,
            event_time=payload.event_time,
            location=payload.location,
            remark=payload.reason,
            source=payload.source,
        )
        lines = await DispatchLineRepository(session, tenant).for_dispatch(dispatch.id)

    return dispatch_detail(dispatch, lines)


async def confirm_delivery(
    session: AsyncSession,
    tenant: TenantContext,
    dispatch_id: uuid.UUID,
    payload: DeliveryConfirmation,
) -> DispatchDetail:
    """Record that the receiver has the goods.

    Stamps the arrival date, names who confirmed it and appends the tracking
    event, all in one transaction. A consignment that arrived after its promised
    date must say why: an on-time-delivery report whose late rows carry no
    explanation is a number nobody can act on.
    """
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        before = snapshot(dispatch, DISPATCH_AUDITED_FIELDS)

        arrival = payload.actual_delivery_date or today()
        if arrival < dispatch.dispatch_date:
            raise BusinessRuleError(
                f"{dispatch.code} left on {dispatch.dispatch_date}; it cannot have arrived on "
                f"{arrival}.",
                details={
                    "dispatch_date": str(dispatch.dispatch_date),
                    "actual_delivery_date": str(arrival),
                },
            )

        reason = payload.delay_reason or dispatch.delay_reason
        late = assert_delay_explained(
            code=dispatch.code,
            expected_delivery_date=dispatch.expected_delivery_date,
            actual_delivery_date=arrival,
            reason=reason,
        )

        dispatch.actual_delivery_date = arrival
        dispatch.delivery_confirmed_by = tenant.user_id
        if payload.delivery_note is not None:
            dispatch.delivery_note = payload.delivery_note
        if reason is not None:
            dispatch.delay_reason = reason

        await _transition(
            session,
            tenant,
            dispatch,
            DispatchStatus.DELIVERED,
            reason=payload.delivery_note or "Delivery confirmed",
            repo=repo,
        )
        await _record_event(
            session,
            tenant,
            dispatch,
            event_status=DispatchStatus.DELIVERED,
            event_time=payload.event_time,
            location=payload.location,
            remark=payload.delivery_note,
            source=payload.source,
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.DISPATCH_CHANGE,
            entity_type=AUDIT_DISPATCH,
            entity_id=dispatch.id,
            entity_code=dispatch.code,
            summary=f"Delivery of {dispatch.code} confirmed on {arrival}",
            user_id=tenant.user_id,
            old_values=before,
            new_values=snapshot(dispatch, DISPATCH_AUDITED_FIELDS),
            context={
                "arrived_late": late,
                "delay_days": delay_days(dispatch.expected_delivery_date, arrival),
            },
        )
        lines = await DispatchLineRepository(session, tenant).for_dispatch(dispatch.id)

    return dispatch_detail(dispatch, lines)


async def add_tracking_event(
    session: AsyncSession, tenant: TenantContext, dispatch_id: uuid.UUID, payload: TrackingEventIn
) -> TrackingEventRead:
    """Record one reported position or status.

    An event whose status differs from the consignment's current one also moves
    it, through the state machine - so a courier feed cannot walk a consignment
    into a status the lifecycle does not allow. An event repeating the current
    status is a position update and changes nothing but the history.

    A status in the delivered half with no arrival date on the row stamps one
    from the event's own time: the table refuses a delivered consignment that
    cannot say when it arrived, and an integration has no other way to supply it.
    """
    repo = DispatchRepository(session, tenant)

    async with transaction(session):
        dispatch = await _lock_dispatch(repo, dispatch_id)
        current = DispatchStatus(dispatch.status)
        target = payload.event_status
        event_time = payload.event_time or utcnow()

        if target is not current:
            arrival = dispatch.actual_delivery_date or max(
                event_time.date(), dispatch.dispatch_date
            )
            if target is DispatchStatus.DELIVERED:
                # The same rule confirm_delivery applies. An event carries no
                # delay reason of its own, so a late arrival must already have
                # one on the row - or be confirmed through the delivery form.
                assert_delay_explained(
                    code=dispatch.code,
                    expected_delivery_date=dispatch.expected_delivery_date,
                    actual_delivery_date=arrival,
                    reason=dispatch.delay_reason,
                )
            if target in DELIVERED_STATUSES and dispatch.actual_delivery_date is None:
                dispatch.actual_delivery_date = arrival
            await _transition(
                session,
                tenant,
                dispatch,
                target,
                reason=payload.remark or f"Tracking event from {payload.source}",
                repo=repo,
            )

        event = await _record_event(
            session,
            tenant,
            dispatch,
            event_status=target,
            event_time=event_time,
            location=payload.location,
            remark=payload.remark,
            source=payload.source,
        )

    return TrackingEventRead.model_validate(event)


# -----------------------------------------------------------------------------
# Delay detection - the scheduled sweep
# -----------------------------------------------------------------------------


async def scan_delays(
    session: AsyncSession,
    *,
    as_of: date | None = None,
    limit: int = MAX_DELAY_SCAN,
) -> list[DelayScanResult]:
    """Find overdue consignments across every tenant and alert their owners.

    Called by the ``notifications.scan_dispatch_delays`` beat task, which has no
    principal and therefore no ``TenantContext`` - this is the one entry point in
    the module that starts from a cross-tenant read (see
    :func:`~src.modules.dispatch.repository.scan_delayed_across_tenants`) and it
    immediately partitions the result by company so every write is scoped again.

    One alert per company, not one per consignment. The recipients are resolved
    from the role assignment at the moment of the sweep, and the ``group_key``
    collapses the hourly re-run onto the same unread bell row rather than adding
    one every hour until somebody mutes the type.

    ASSUMPTION: the audit trail records one entry per company per sweep, naming
    the consignments flagged in its context, rather than one entry per row. A
    row-per-consignment trail of an automated hourly job would bury the human
    actions the audit log exists to make findable, and the fact being recorded -
    "these were flagged, and these people were told" - is one event.

    Each company's work runs in its own nested transaction so one tenant's
    failure cannot roll back the alerts already raised for another.
    """
    moment = as_of or today()
    rows = await scan_delayed_across_tenants(session, as_of=moment, limit=limit)
    if not rows:
        return []

    by_company: dict[uuid.UUID, list[DelayedDispatchRow]] = {}
    for row in rows:
        by_company.setdefault(row.company_id, []).append(row)

    results: list[DelayScanResult] = []
    for company_id, delayed in by_company.items():
        try:
            notified = await _raise_delay_alert(session, company_id, delayed, as_of=moment)
        except Exception as exc:
            log.error(
                "dispatch.delay_scan_failed",
                company_id=str(company_id),
                delayed=len(delayed),
                error=str(exc),
                exc_info=exc,
            )
            continue
        results.append(
            DelayScanResult(
                company_id=company_id, delayed_count=len(delayed), notified_count=notified
            )
        )

    return results


async def _raise_delay_alert(
    session: AsyncSession,
    company_id: uuid.UUID,
    delayed: Sequence[DelayedDispatchRow],
    *,
    as_of: date,
) -> int:
    """Alert one company about its overdue consignments and mark them notified."""
    worst = max(delayed, key=lambda row: row.delay_days)
    headline = (
        f"{len(delayed)} consignment(s) are past their promised delivery date"
        if len(delayed) > 1
        else f"Consignment {worst.code} is past its promised delivery date"
    )
    body = (
        f"{worst.code} was promised for {worst.expected_delivery_date} and is "
        f"{worst.delay_days} day(s) overdue"
        + (f" ({worst.party_name})" if worst.party_name else "")
        + ". Open the dispatch tracking board to review the full list."
    )

    async with transaction(session):
        recipients = await notify_role(
            session,
            company_id=company_id,
            role_codes=DELAY_ALERT_ROLES,
            notification_type=NotificationType.DISPATCH_DELAYED,
            title=headline,
            body=body,
            severity=NotificationSeverity.WARNING,
            entity_type=AUDIT_DISPATCH,
            entity_id=worst.dispatch_id,
            action_url="/dispatch/delayed",
            # One bell row per company that the hourly sweep updates in place.
            group_key=f"dispatch-delay:{company_id}",
        )

        dispatch_ids = [row.dispatch_id for row in delayed]
        await mark_delay_notified(session, company_id=company_id, dispatch_ids=dispatch_ids)

        await record_audit(
            session,
            company_id=company_id,
            action=AuditAction.DISPATCH_CHANGE,
            entity_type=AUDIT_DISPATCH,
            entity_id=worst.dispatch_id,
            entity_code=worst.code,
            summary=f"Delay sweep flagged {len(delayed)} overdue consignment(s)",
            user_id=None,
            actor_label="notifications.scan_dispatch_delays",
            context={
                "as_of": as_of.isoformat(),
                "delayed_codes": [row.code for row in delayed],
                "worst_delay_days": worst.delay_days,
                "recipients_notified": len(recipients),
            },
        )

    log.info(
        "dispatch.delays_flagged",
        company_id=str(company_id),
        delayed=len(delayed),
        notified=len(recipients),
    )
    return len(recipients)


__all__ = [
    "AUDIT_DISPATCH",
    "AUDIT_SALES_ORDER",
    "DELAY_ALERT_ROLES",
    "DISPATCHABLE_ORDER_STATUSES",
    "DISPATCH_LIFECYCLE",
    "MOVEMENT_REFERENCE_DISPATCH",
    "SERIAL_RESERVATION_MESSAGE",
    "LineBalance",
    "add_tracking_event",
    "arrived_late",
    "assert_delay_explained",
    "assert_order_dispatchable",
    "build_line_movements",
    "cancel_dispatch",
    "confirm_delivery",
    "create_dispatch",
    "delay_days",
    "delete_draft_dispatch",
    "dispatch_balance",
    "dispatch_consignment",
    "dispatch_detail",
    "get_dispatch_detail",
    "grn_is_pending",
    "is_delayed",
    "list_delayed",
    "list_dispatches",
    "list_tracking_events",
    "mark_in_transit",
    "mark_ready",
    "qc_is_pending",
    "sales_order_status_path",
    "scan_delays",
    "tracking_board",
    "update_dispatch",
]
