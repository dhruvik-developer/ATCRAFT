"""The inventory engine: the only place stock is allowed to change.

Master rules section 10, database contract section 4.4. Every stock-changing
operation in the whole ERP - goods receipt, material issue, production output,
job work, dispatch, QC decision, transfer, adjustment, count - ends up in
:meth:`MovementEngine.post_with_balances`, and that method always does the same
seven things inside one transaction:

1. ``SET LOCAL lock_timeout`` so a contended posting fails fast with a retryable
   error instead of pinning an HTTP worker behind someone else's transaction;
2. create-if-missing and ``SELECT ... FOR UPDATE`` every affected balance row,
   **in primary-key order**, so two multi-line documents touching the same items
   queue instead of deadlocking;
3. re-read the quantities *after* the lock - a value read before it is worthless;
4. validate against the freshly-read values and raise
   :class:`~src.core.errors.InsufficientStockError` if short;
5. write the ledger rows;
6. update the balances and the FIFO cost layers behind them;
7. write one audit entry per movement.

Either all of that commits or none of it does. There is no path that writes a
ledger row without a balance, or a balance without a ledger row.

Why the sign of a movement is not the caller's decision
-------------------------------------------------------
``TRANSACTION_SIGNS`` fixes the sign per transaction type. A caller supplies a
magnitude; the engine applies the sign. A "receipt" that arrives with a negative
quantity is a mis-keyed document, and silently treating it as an issue is how a
warehouse ends up with stock nobody can account for.

Why QC hold does not change on-hand
------------------------------------
``available_quantity`` is a generated column: ``on_hand - reserved - qc_hold -
damaged``. That expression only holds if ``qc_hold`` and ``damaged`` are subsets
of on-hand. So placing stock on QC hold raises ``qc_hold_quantity`` and leaves
``on_hand_quantity`` alone - the goods have not moved, they have only stopped
being usable. The ledger row still carries the negative sign the vocabulary
fixes for ``QC_HOLD``, because the sign describes the effect on the *available*
pool, which is what every other module reads.

Whether a *stored* ledger row moved on-hand is not a property of its type alone:
a caller that names a ``stock_state`` or ``from_state`` outside the on-hand pool
makes other types neutral too. Each row therefore records the source pool it
came from, and ``LedgerRepository.posted_quantity`` recomputes the effect rather
than guessing it from the type.

Valuation
---------
**FIFO, with real cost layers.** Every movement that adds to on-hand opens a row
in ``inventory_cost_layers`` carrying the quantity received and the price paid;
every movement that takes stock out consumes layers oldest first and costs what
those layers actually cost. ``inventory_cost_consumptions`` records which layers
each issue drew on, so the cost of goods issued is auditable back to the receipt
that priced it.

The ordering is the method. Layers are consumed by ``(received_at, id)`` - never
by timestamp alone, because PostgreSQL's ``now()`` is transaction *start* time
and every layer opened by one document shares it, so the UUIDv7 primary key is
what makes the order total (CLAUDE.md). Consuming in an arbitrary order would
still add up; it just would not be FIFO.

``inventory_balances.average_cost`` survives as a **derived display figure** -
``stock_value / on_hand_quantity``, recomputed from the layers on every posting -
because that is what a stock report and the finance valuation mean by "cost per
unit". It is never an independent moving average: two valuations maintained side
by side disagree the first time a price moves, and then nobody can say which one
the balance sheet is.

Outbound movements are still never priced by the caller. A supplied cost is used
only for an arrival, or for a unit no layer can account for.

**Scrap keeps a queue of its own** (``CostLayerPool.SCRAP``). A movement into
the scrap pool charges its cost exactly as before - the write-off happens when
stock is scrapped - and also opens scrap layers at the costs it charged. Those
layers carry no book value and are in no valuation; they exist so the one
movement that takes stock out of scrap, ``SCRAP_DISPOSAL``, is valued from
what the scrap really cost, oldest first, rather than from whatever usable
stock happens to stand beside it (docs/assumptions.md §E8-2).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeVar

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from src.common.codes import DocTypes, next_document_number
from src.common.warehouse_scope import assert_warehouse_scope
from src.core.clock import today, utcnow
from src.core.database import advisory_xact_lock, set_lock_timeout, transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InsufficientStockError,
    NotFoundError,
    PermissionDeniedError,
    StockLockTimeoutError,
    ValidationError,
)
from src.core.ids import new_id, persisted_actor
from src.core.logging import get_logger
from src.core.messages import NO_FIELDS_TO_UPDATE, inactive_record
from src.core.observability import insufficient_stock_events, inventory_conflicts
from src.core.pagination import CursorParams, PageParams, SortParams, paginate, paginate_keyset
from src.core.permissions import Permission
from src.core.responses import CursorPagination, OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import to_money, to_quantity, to_unit_price
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.inventory.models import (
    LAYER_ORIGIN_RECONCILIATION,
    ON_HAND_STATES,
    SERIAL_IN_WAREHOUSE_STATUSES,
    STATE_BUCKETS,
    TRANSACTION_SIGNS,
    AdjustmentReason,
    AdjustmentStatus,
    BatchStatus,
    BinMove,
    BinMoveLine,
    BinMoveStatus,
    CostLayerPool,
    InternalIssue,
    InternalIssueLine,
    InternalIssueStatus,
    InventoryBalance,
    InventoryCostLayer,
    InventoryCostLayerConsumption,
    InventoryTransaction,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalCountStatus,
    ScrapDisposal,
    ScrapDisposalLine,
    ScrapDisposalStatus,
    SerialStatus,
    StockAdjustment,
    StockAdjustmentLine,
    StockBatch,
    StockSerial,
    StockSerialConversion,
    StockState,
    StockTransfer,
    StockTransferLine,
    TransactionType,
    TransferStatus,
    fifo_sort_key,
)
from src.modules.inventory.repository import (
    BalanceGrain,
    BatchFacts,
    BinMoveLineRepository,
    BinMoveRepository,
    CostConsumptionRepository,
    CostLayerRepository,
    InternalIssueLineRepository,
    InternalIssueRepository,
    InventoryRepository,
    ItemFacts,
    LedgerRepository,
    LocationFacts,
    PhysicalCountLineRepository,
    PhysicalCountRepository,
    ScrapDisposalLineRepository,
    ScrapDisposalRepository,
    StockAdjustmentLineRepository,
    StockAdjustmentRepository,
    StockBatchRepository,
    StockSerialRepository,
    StockTransferLineRepository,
    StockTransferRepository,
    WarehouseFacts,
)
from src.modules.inventory.schemas import (
    AdjustmentFilters,
    AvailabilityRead,
    BalanceFilters,
    BalanceReadWithCost,
    BinMoveCreate,
    BinMoveFilters,
    BinMoveLineReadWithCost,
    BinMoveReadWithCost,
    InternalIssueCreate,
    InternalIssueFilters,
    InternalIssueLineReadWithCost,
    InternalIssueReadWithCost,
    ItemStockSummaryWithCost,
    LedgerEntryRead,
    LedgerFilters,
    PhysicalCountCreate,
    PhysicalCountEntryRequest,
    PhysicalCountFilters,
    PhysicalCountLineRead,
    PhysicalCountRead,
    ScrapDisposalCreate,
    ScrapDisposalFilters,
    ScrapDisposalLineReadWithCost,
    ScrapDisposalReadWithCost,
    StockAdjustmentCreate,
    StockAdjustmentLineCreate,
    StockAdjustmentLineRead,
    StockAdjustmentRead,
    StockAdjustmentUpdate,
    StockBatchAction,
    StockBatchCreate,
    StockBatchRead,
    StockSerialBulkCreate,
    StockSerialConversionRead,
    StockSerialCreate,
    StockSerialDetail,
    StockSerialRead,
    StockTransferCreate,
    StockTransferLineCreate,
    StockTransferLineRead,
    StockTransferRead,
    StockTransferUpdate,
    TransferFilters,
    WarehouseStockRowWithCost,
)
from src.modules.organization.service import require_cost_centre, require_department

log = get_logger(__name__)

# --- tuning constants --------------------------------------------------------

#: How long a posting waits for a contended balance row before giving up.
#: Short on purpose: an operator who waits five seconds retries; an HTTP worker
#: blocked for thirty is an outage in the making (section 30).
LOCK_TIMEOUT_MS = 5_000

# ASSUMPTION: a stock adjustment worth more than this must be approved by a
# second person before it posts. The master rules require an approval threshold
# but do not fix the number (docs/assumptions.md, open question 2). It is a named
# constant so raising it is a one-line, reviewable change rather than a hunt
# through the service.
ADJUSTMENT_APPROVAL_VALUE_THRESHOLD = Decimal("25000.00")

# ASSUMPTION: an internal issue worth more than this - measured on the FIFO
# cost it will really consume - is approved by a second person before it posts.
# The same figure as the adjustment threshold, for the same reason: stock
# leaving the books with no production order or customer behind it is exactly
# the movement that needs a second pair of eyes, and the master rules name a
# threshold without fixing it (docs/assumptions.md §E8-3, open question 2).
INTERNAL_ISSUE_APPROVAL_VALUE = Decimal("25000.00")

# ASSUMPTION: a count sheet larger than this is refused. A person cannot work
# through 20,000 lines in one session, and building the sheet would hold a
# transaction open long enough to matter. Narrow the scope by location or item
# instead - which is what cycle counting means.
MAX_COUNT_LINES = 5_000

AUDIT_ENTITY_MOVEMENT = "inventory_transaction"
AUDIT_ENTITY_BALANCE = "inventory_balance"
AUDIT_ENTITY_TRANSFER = "stock_transfer"
AUDIT_ENTITY_ADJUSTMENT = "stock_adjustment"
AUDIT_ENTITY_COUNT = "physical_count"
AUDIT_ENTITY_BATCH = "stock_batch"
AUDIT_ENTITY_SERIAL = "stock_serial"
#: Also the ledger ``reference_type`` of the movements each document posts.
AUDIT_ENTITY_BIN_MOVE = "bin_move"
AUDIT_ENTITY_SCRAP_DISPOSAL = "scrap_disposal"
AUDIT_ENTITY_INTERNAL_ISSUE = "internal_issue"

#: PostgreSQL SQLSTATE for "could not obtain lock" - what lock_timeout raises.
_LOCK_NOT_AVAILABLE = "55P03"

#: The scale ``inventory_balances.stock_value`` is actually stored at.
#: ``Money`` is ``NUMERIC(18, 4)`` (``src.core.types``) while ``to_money``
#: quantizes to *two* places, because two is the scale a document total is
#: presented at. Quantizing a position's value with ``to_money`` therefore threw
#: away two places the column was holding, and did it once per position - so the
#: same stock was worth a different amount depending on how many warehouses,
#: bins or batches it happened to be split across. Rounded once, here, at the
#: column's own scale.
STOCK_VALUE_QUANTUM = Decimal("0.0001")


def to_stock_value(value: Decimal | int | str) -> Decimal:
    """Round a position's value once, at the scale its column stores.

    Not ``to_money``: see :data:`STOCK_VALUE_QUANTUM`. Still a rounding - four
    places cannot hold the exact product of a four-place quantity and a
    six-place unit price - which is why nothing in this module claims
    ``stock_value`` is exact any more.
    """
    return Decimal(value).quantize(STOCK_VALUE_QUANTUM, rounding=ROUND_HALF_UP)


#: Balance columns a movement can change, in the order violations are reported.
_POOL_COLUMNS = (
    "qc_hold_quantity",
    "damaged_quantity",
    "in_transit_quantity",
    "job_work_quantity",
    "scrap_quantity",
)


# -----------------------------------------------------------------------------
# Pure movement arithmetic - unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MovementRequest:
    """One line of a batch posting.

    ``quantity`` is a magnitude. The sign comes from ``transaction_type``; see
    :func:`signed_quantity`.
    """

    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: Decimal
    transaction_type: TransactionType
    reference_type: str
    reason: str
    reference_id: uuid.UUID | None = None
    reference_code: str | None = None
    location_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    serial_id: uuid.UUID | None = None
    unit_cost: Decimal | None = None
    stock_state: StockState = StockState.AVAILABLE
    #: Only set by :func:`change_stock_state`, which moves a quantity from one
    #: pool to another. Everything else leaves it None and takes the source pool
    #: from the transaction type's default.
    from_state: StockState | None = None
    #: This movement *completes* a transfer whose stock is already in transit -
    #: it books goods that have physically left their source and are standing at
    #: the destination. Only :meth:`StockTransferService.receive` sets it, and
    #: :func:`validate_movement` reads it to decide which rules still apply. The
    #: in-transit note above :func:`validate_movement` says why.
    completes_in_transit: bool = False
    #: The exact FIFO cost breakdown this **arrival** opens, instead of one
    #: layer at ``unit_cost``.
    #:
    #: Set by :meth:`StockTransferService.receive`, which replays the layers its
    #: own dispatch consumed. Stock moving between two of the company's
    #: warehouses has to carry its cost with it: re-costing the arrival from an
    #: average - the destination's, or a blend of the source's - changes what
    #: the company's stock is worth purely by moving a box, and re-averaging a
    #: two-layer draw also destroys the order the destination will consume it
    #: in. Ignored on a movement that does not raise on-hand.
    cost_layers: tuple[LayerCost, ...] = ()
    #: The same thing for a breakdown that does not exist yet when the batch is
    #: assembled: the zero-based index, **in this batch**, of the outbound
    #: movement whose consumed layers this arrival re-opens.
    #:
    #: :func:`transfer_stock` is the only caller. Its two legs are posted in one
    #: batch so the two positions are locked together, and the outbound leg's
    #: layer draw is not known until it has been applied - so it is named by
    #: position rather than passed as a value. Validated in
    #: :meth:`MovementEngine.post_with_balances`: it must point backwards, at a
    #: movement that lowers on-hand, from one that raises it.
    carries_cost_of: int | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        # A frozen dataclass validates here or nowhere. The exemption
        # ``completes_in_transit`` grants is narrow on purpose, so a caller that
        # sets it on anything but the inbound leg of a transfer is a programming
        # error and has to be loud rather than quietly privileged.
        if self.completes_in_transit and (
            TransactionType(self.transaction_type) is not TransactionType.TRANSFER_IN
        ):
            raise ValidationError(
                "Only the inbound leg of a stock transfer can complete an in-transit quantity.",
                details={"transaction_type": str(self.transaction_type)},
            )

    @property
    def grain(self) -> BalanceGrain:
        return BalanceGrain(
            item_id=self.item_id,
            warehouse_id=self.warehouse_id,
            location_id=self.location_id,
            batch_id=self.batch_id,
        )


@dataclass(frozen=True, slots=True)
class BalanceEffect:
    """How one movement changes each balance column, as a multiplier of the magnitude."""

    on_hand_quantity: int = 0
    qc_hold_quantity: int = 0
    damaged_quantity: int = 0
    in_transit_quantity: int = 0
    job_work_quantity: int = 0
    scrap_quantity: int = 0

    def deltas(self, magnitude: Decimal) -> dict[str, Decimal]:
        return {
            f.name: magnitude * getattr(self, f.name) for f in fields(self) if getattr(self, f.name)
        }


@dataclass(frozen=True, slots=True)
class _Transition:
    """Where a movement type takes stock from, and where it puts it.

    ``None`` on either end means "outside this balance position" - a purchase
    receipt comes from outside the company, a dispatch goes to a customer.
    ``qualifies`` records which end a caller-supplied ``stock_state`` overrides.
    """

    source: StockState | None
    target: StockState | None
    qualifies: str  # "source" or "target"


#: The default pool transition of every movement type. This table plus
#: :func:`movement_effect` is the entire definition of what a movement does to a
#: balance - there is no second place where buckets are adjusted.
_DEFAULT_TRANSITION: dict[TransactionType, _Transition] = {
    # Increases: stock arrives from outside the position.
    TransactionType.PURCHASE_RECEIPT: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.PRODUCTION_RECEIPT: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.ASSEMBLY_RECEIPT: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.MATERIAL_RETURN: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.CUSTOMER_RETURN: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.OPENING_BALANCE: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.ADJUSTMENT_INCREASE: _Transition(None, StockState.AVAILABLE, "target"),
    TransactionType.TRANSFER_IN: _Transition(None, StockState.AVAILABLE, "target"),
    # A job-work receipt is usually a *different* item coming back (castings go
    # out, machined parts come in), so it defaults to a plain increase. Returning
    # the same item unchanged is change_stock_state(JOB_WORK -> AVAILABLE), which
    # supplies the source pool explicitly.
    TransactionType.JOB_WORK_RECEIPT: _Transition(None, StockState.AVAILABLE, "target"),
    # Releasing held stock. The source defaults to the QC hold pool because that
    # is what a release almost always empties; change_stock_state names another
    # source explicitly when it is releasing from damaged instead.
    TransactionType.QC_RELEASE: _Transition(StockState.QC_HOLD, StockState.AVAILABLE, "target"),
    # Decreases: stock leaves the position.
    TransactionType.MATERIAL_ISSUE: _Transition(StockState.AVAILABLE, None, "source"),
    TransactionType.ASSEMBLY_CONSUME: _Transition(StockState.AVAILABLE, None, "source"),
    TransactionType.DISPATCH: _Transition(StockState.AVAILABLE, None, "source"),
    TransactionType.PURCHASE_RETURN: _Transition(StockState.AVAILABLE, None, "source"),
    TransactionType.ADJUSTMENT_DECREASE: _Transition(StockState.AVAILABLE, None, "source"),
    TransactionType.TRANSFER_OUT: _Transition(StockState.AVAILABLE, None, "source"),
    # Stock that leaves on-hand but stays company property.
    TransactionType.JOB_WORK_ISSUE: _Transition(
        StockState.AVAILABLE, StockState.JOB_WORK, "target"
    ),
    TransactionType.SCRAP: _Transition(StockState.AVAILABLE, StockState.SCRAP, "target"),
    # Holding stock does not move it - see the module docstring.
    TransactionType.QC_HOLD: _Transition(StockState.AVAILABLE, StockState.QC_HOLD, "target"),
    # The one way out of scrap - see TransactionType.SCRAP_DISPOSAL.
    TransactionType.SCRAP_DISPOSAL: _Transition(StockState.SCRAP, None, "source"),
}

#: Types whose caller-supplied ``stock_state`` names the pool the quantity
#: *lands* in. For every other type it names the pool it was taken *from*.
#: Derived from the transition table, so anything reading stored ledger rows -
#: the scrap register - asks the same question the engine answered when it
#: posted them, and a new type cannot be classified twice.
TARGET_QUALIFIED_TYPES: frozenset[TransactionType] = frozenset(
    transaction_type
    for transaction_type, transition in _DEFAULT_TRANSITION.items()
    if transition.qualifies == "target"
)
#: Types that land in scrap when the caller names no pool.
SCRAP_LANDING_TYPES: frozenset[TransactionType] = frozenset(
    transaction_type
    for transaction_type, transition in _DEFAULT_TRANSITION.items()
    if transition.target is StockState.SCRAP
)
#: Types that draw on scrap when the caller names no pool.
SCRAP_DRAWING_TYPES: frozenset[TransactionType] = frozenset(
    transaction_type
    for transaction_type, transition in _DEFAULT_TRANSITION.items()
    if transition.source is StockState.SCRAP
)


def signed_quantity(transaction_type: TransactionType, quantity: Decimal) -> Decimal:
    """Apply the transaction type's fixed sign to a movement quantity.

    Callers pass a magnitude. A quantity that is already negative is accepted
    only when the type is negative anyway, which keeps a re-posted ledger row
    idempotent; a *negative receipt* is rejected outright, because it is always
    a mis-keyed document rather than a compact way of writing an issue.
    """
    # Other modules may pass the bare string the contract shows as the default
    # ("AVAILABLE", "PURCHASE_RECEIPT"). Coercing here means a lookup can never
    # fall through to a KeyError, and an unknown value fails loudly instead.
    transaction_type = TransactionType(transaction_type)

    value = to_quantity(Decimal(quantity))
    if value == 0:
        raise ValidationError(
            "A stock movement of zero changes nothing.",
            details={"transaction_type": str(transaction_type)},
        )

    sign = TRANSACTION_SIGNS[transaction_type]
    if value < 0 and sign > 0:
        raise ValidationError(
            f"{transaction_type} increases stock and cannot carry a negative quantity.",
            details={"transaction_type": str(transaction_type), "quantity": str(value)},
        )
    return to_quantity(abs(value) * sign)


def resolve_pools(
    transaction_type: TransactionType,
    stock_state: StockState = StockState.AVAILABLE,
    from_state: StockState | None = None,
) -> tuple[StockState | None, StockState | None]:
    """The pool this movement takes stock *from*, and the pool it puts it *in*.

    ``stock_state`` of ``AVAILABLE`` means "unspecified" and leaves the type's
    default transition alone; any other value overrides the end of the
    transition that the type declares it qualifies. ``from_state`` always
    overrides the source, and is how :func:`change_stock_state` expresses a
    pool-to-pool move such as QC hold to damaged.

    Split out of :func:`movement_effect` so the serial rules can consult the
    same answer. A ``BalanceEffect`` is deltas by column, and two different
    landing pools can share a column - ``AVAILABLE`` and ``RETURN`` are both the
    residual, with no column of their own - so the effect alone cannot say where
    a movement actually put the goods. The serial lifecycle needs to know: a QC
    release that lands in the available pool ends a customer return, and one
    that lands the quantity back in the return pool does not.
    """
    transaction_type = TransactionType(transaction_type)
    stock_state = StockState(stock_state)
    if from_state is not None:
        from_state = StockState(from_state)

    transition = _DEFAULT_TRANSITION[transaction_type]
    source, target = transition.source, transition.target

    if stock_state is not StockState.AVAILABLE:
        if transition.qualifies == "target":
            target = stock_state
        else:
            source = stock_state
    if from_state is not None:
        source = from_state

    if source is not None and source == target:
        raise ValidationError(
            "That combination of movement type and stock state describes no movement.",
            details={
                "transaction_type": str(transaction_type),
                "stock_state": str(stock_state),
                "from_state": str(from_state) if from_state else None,
            },
        )
    return source, target


def balance_effect(source: StockState | None, target: StockState | None) -> BalanceEffect:
    """Turn a resolved pool-to-pool move into balance-column deltas."""
    deltas: dict[str, int] = {}
    for state, factor in ((source, -1), (target, 1)):
        if state is None:
            continue
        bucket = STATE_BUCKETS[state]
        if bucket is not None:
            deltas[bucket] = deltas.get(bucket, 0) + factor
        if state in ON_HAND_STATES:
            deltas["on_hand_quantity"] = deltas.get("on_hand_quantity", 0) + factor

    return BalanceEffect(**deltas)


def movement_effect(
    transaction_type: TransactionType,
    stock_state: StockState = StockState.AVAILABLE,
    from_state: StockState | None = None,
) -> BalanceEffect:
    """Which balance columns this movement changes, and in which direction.

    The pools themselves come from :func:`resolve_pools`; this turns them into
    the column deltas the balance row is updated with.
    """
    return balance_effect(*resolve_pools(transaction_type, stock_state, from_state))


def state_change_type(from_state: StockState, to_state: StockState) -> TransactionType:
    """The ledger vocabulary term for moving stock between two pools.

    The vocabulary in section 83 is closed, so not every pair is expressible.
    The unsupported ones are refused with the entry point that *does* handle
    them rather than being forced into a misleading transaction type.
    """
    from_state = StockState(from_state)
    to_state = StockState(to_state)

    if from_state is to_state:
        raise ValidationError(
            "The source and target stock states are the same.",
            details={"state": str(from_state)},
        )
    if StockState.IN_TRANSIT in (from_state, to_state):
        raise BusinessRuleError(
            "In-transit stock is managed by the stock transfer document, not by a state change.",
            details={"from_state": str(from_state), "to_state": str(to_state)},
        )
    if from_state is StockState.SCRAP:
        # Bringing scrap back into stock is a write-up with a stated reason, not
        # a quiet reclassification - it changes the value of the balance sheet.
        raise BusinessRuleError(
            "Scrap is not returned to stock by a state change. Post an adjustment "
            "with a reason instead.",
            details={"to_state": str(to_state)},
        )
    if to_state is StockState.SCRAP:
        return TransactionType.SCRAP
    if to_state is StockState.JOB_WORK:
        return TransactionType.JOB_WORK_ISSUE
    if from_state is StockState.JOB_WORK:
        return TransactionType.JOB_WORK_RECEIPT
    if to_state in (StockState.QC_HOLD, StockState.DAMAGED):
        return TransactionType.QC_HOLD
    return TransactionType.QC_RELEASE


@dataclass(frozen=True, slots=True)
class LayerCost:
    """A quantity of stock and what one unit of it cost.

    The unit in which a cost is carried from one place to another: a receipt
    opening a layer, a transfer re-opening at its destination exactly what its
    source gave up. ``value`` is deliberately **unrounded** - callers sum a
    handful of these and round once, because rounding each line and then adding
    is how a transfer starts creating and destroying money.
    """

    quantity: Decimal
    unit_cost: Decimal

    @property
    def value(self) -> Decimal:
        return self.quantity * self.unit_cost


@dataclass(frozen=True, slots=True)
class LayerView:
    """One open cost layer, as the arithmetic sees it: a quantity and a price.

    Deliberately not the ORM row. The FIFO maths is the part that has to be
    right, and it is testable to the last paisa without a database when it takes
    plain numbers - which is what the pure-function tests do.
    """

    remaining: Decimal
    unit_cost: Decimal


@dataclass(frozen=True, slots=True)
class LayerDraw:
    """How much of one layer an outbound movement took, and at what price.

    ``index`` addresses the layer in the sequence handed to
    :func:`consume_layers`, so the caller can map a draw back to the row it has
    to decrement without the arithmetic ever touching an ORM object.
    """

    index: int
    quantity: Decimal
    unit_cost: Decimal

    @property
    def value(self) -> Decimal:
        return self.quantity * self.unit_cost


@dataclass(frozen=True, slots=True)
class LayerConsumption:
    """What one outbound movement drew, layer by layer."""

    draws: tuple[LayerDraw, ...]
    covered_quantity: Decimal
    #: Quantity the layers could not account for. Zero in a healthy position -
    #: see :func:`consume_layers` for the only way it is not.
    uncovered_quantity: Decimal
    uncovered_unit_cost: Decimal

    @property
    def layer_value(self) -> Decimal:
        """Value actually taken *out of layers*. Unrounded."""
        return sum((draw.value for draw in self.draws), Decimal("0"))

    @property
    def value(self) -> Decimal:
        """What the movement cost, including any quantity no layer covered."""
        return self.layer_value + self.uncovered_quantity * self.uncovered_unit_cost


def open_layer_value(layers: Sequence[LayerView]) -> Decimal:
    """Exact value of a position's open layers. Unrounded on purpose."""
    return sum((layer.remaining * layer.unit_cost for layer in layers), Decimal("0"))


def open_layer_quantity(layers: Sequence[LayerView]) -> Decimal:
    return sum((layer.remaining for layer in layers), Decimal("0"))


def position_cost_basis(layers: Sequence[LayerView]) -> Decimal:
    """What one unit of what is actually on the shelf cost, on average.

    The display figure behind ``inventory_balances.average_cost``, and the
    fallback price for the movements that have to name a cost but are not
    themselves buying anything - a QC hold, a re-book of stock coming back from
    a job-work vendor. It is derived from the layers every time it is asked for
    and is never maintained as a running number of its own; a second, separately
    maintained average is precisely how a codebase ends up with two valuations
    that disagree.
    """
    quantity = open_layer_quantity(layers)
    if quantity <= 0:
        return Decimal("0.000000")
    return to_unit_price(open_layer_value(layers) / quantity)


def consume_layers(
    layers: Sequence[LayerView],
    quantity: Decimal,
    *,
    uncovered_unit_cost: Decimal,
) -> LayerConsumption:
    """Take ``quantity`` out of ``layers``, oldest first.

    ``layers`` must already be in FIFO order - :func:`fifo_sort_key` decides
    that, and the ordering is the whole method: consume them in a different
    sequence and this is no longer FIFO, it is an arbitrary pick that happens to
    add up.

    Partial consumption is the normal case. 30 against ``[20 @ 100, 50 @ 120]``
    empties the first layer and takes 10 of the second, and the movement cost
    ``20*100 + 10*120 = 3 200`` - not 30 times any average. The emptied layer is
    *exhausted*, not deleted: the caller leaves the row in place with
    ``remaining_quantity`` at zero, because the consumption rows point at it and
    it is the evidence for what that issue cost.

    ASSUMPTION: ``uncovered_quantity`` is what the layers could not account
    for, and it is priced rather than refused. In a healthy position it is
    always zero: on-hand can never go negative (the check
    constraint, not just the service), and every movement that changes on-hand
    changes the layers by the same amount, so the two cannot come apart. It is
    not zero when a position's layers were never built - stock that predates
    FIFO valuation and that the migration's backfill did not reach, or a
    movement posted by an older deployment between the backfill and the rollout.
    ``MovementEngine._reconcile_layers`` closes that gap before it can be hit;
    this stays as the floor beneath it, because valuing the uncovered part at
    the position's own last known cost is a bounded, visible error, while
    raising here would refuse a movement of stock that is physically present.
    """
    outstanding = to_quantity(quantity)
    if outstanding <= 0:  # pragma: no cover - callers pass a positive magnitude
        raise ValidationError(
            "A consumption of zero takes nothing from the cost layers.",
            details={"quantity": str(quantity)},
        )

    draws: list[LayerDraw] = []
    for index, layer in enumerate(layers):
        if outstanding <= 0:
            break
        available = to_quantity(layer.remaining)
        if available <= 0:
            continue
        taken = available if available <= outstanding else outstanding
        draws.append(
            LayerDraw(index=index, quantity=taken, unit_cost=to_unit_price(layer.unit_cost))
        )
        outstanding -= taken

    return LayerConsumption(
        draws=tuple(draws),
        covered_quantity=to_quantity(quantity) - outstanding,
        uncovered_quantity=outstanding,
        uncovered_unit_cost=to_unit_price(uncovered_unit_cost),
    )


def _first_positive(*candidates: Decimal | None) -> Decimal:
    for candidate in candidates:
        if candidate is not None and candidate > 0:
            return to_unit_price(candidate)
    return Decimal("0.000000")


def inbound_unit_cost(
    *,
    movement_unit_cost: Decimal | None,
    fallback_cost: Decimal,
    position_basis: Decimal,
    average_before: Decimal,
) -> Decimal:
    """What one unit of an arrival costs when the layer to open needs a price.

    ASSUMPTION: the precedence is the caller's stated cost, then the item's
    standard cost, then what the position's remaining stock cost, then its last
    derived cost. It is the precedence the weighted average used before FIFO and
    it is kept deliberately: FIFO changes which cost *leaves* a position, not
    where an unpriced arrival gets its price from, and reordering it here would
    silently re-value every module that posts a receipt without naming a cost -
    production output, assembly output, a QC release.

    What that means for a round trip is worth stating rather than hiding. A
    document that knows what it issued passes that cost back and the round trip
    is exactly value-neutral - production returns do this, and so does a stock
    transfer, which replays the source's own layers at the destination. A
    movement that returns stock without naming a cost is re-booked at standard
    cost, so the position's value changes by the difference between standard
    cost and what the goods originally cost. The alternative - re-booking at the
    position's current basis - would be value-neutral for a return and wrong for
    a production receipt, which genuinely costs what it costs and not what the
    stock beside it cost. Callers that know the number should pass it.
    """
    return _first_positive(movement_unit_cost, fallback_cost, position_basis, average_before)


def outbound_unit_cost(
    *,
    position_basis: Decimal,
    average_before: Decimal,
    movement_unit_cost: Decimal | None,
    fallback_cost: Decimal,
) -> Decimal:
    """What a unit costs that the layers could not account for.

    Only ever reached for the uncovered remainder described in
    :func:`consume_layers`, and for the movements that change no on-hand at all
    but still have to put a number on the ledger row - a QC hold, a scrapping of
    stock already at a job-work vendor.

    ASSUMPTION unchanged from the weighted average: an outbound movement ignores
    any unit cost the caller supplies while the position has a cost of its own.
    A supplied cost is used only when there is nothing to draw on. Letting the
    caller's number through would let a document drain a position of value by
    quoting a low price.
    """
    return _first_positive(position_basis, average_before, movement_unit_cost, fallback_cost)


def derive_average_cost(
    *, stock_value: Decimal, on_hand: Decimal, previous_average: Decimal
) -> Decimal:
    """``inventory_balances.average_cost``: value over quantity, nothing else.

    A *display* figure, recomputed from the layers on every posting. It exists
    because "cost per unit" is what a stock report and a valuation export mean,
    and four things outside this module read it - but it is never allowed to
    become an average maintained by its own arithmetic beside the layers, which
    would give the system two answers to the same question.

    An emptied position keeps its previous figure: there are no layers left to
    derive from, so there is no new information, and the last known cost is what
    an adjustment or a count re-books stock at. ``stock_value`` is zero either
    way, so nothing on the balance sheet turns on it.
    """
    if on_hand > 0:
        return to_unit_price(stock_value / on_hand)
    return to_unit_price(previous_average)


@dataclass(frozen=True, slots=True)
class Valuation:
    """Result of costing one movement against a position's FIFO layers."""

    #: The ledger row's unit cost. For a multi-layer draw this is the blend -
    #: total over quantity - because a ledger row carries one price. The
    #: unblended truth is in ``draws``.
    unit_cost: Decimal
    total_cost: Decimal
    #: The balance's derived display cost after the movement.
    average_cost: Decimal
    #: The value of the position's open layers after the movement, summed
    #: unrounded and rounded once at the scale ``inventory_balances.stock_value``
    #: stores - see :func:`to_stock_value`. Not exact, and no longer claimed to
    #: be: four places cannot hold the product of a four-place quantity and a
    #: six-place price.
    stock_value: Decimal
    #: Layers this movement opens.
    opened: tuple[LayerCost, ...] = ()
    #: Layers this movement consumed, by index into the sequence it was given.
    draws: tuple[LayerDraw, ...] = ()
    #: Quantity no layer could account for - see :func:`consume_layers`.
    uncovered_quantity: Decimal = Decimal("0")
    #: What one unit of ``uncovered_quantity`` was priced at.
    uncovered_unit_cost: Decimal = Decimal("0")


def value_movement(
    *,
    on_hand_after: Decimal,
    magnitude: Decimal,
    on_hand_delta: Decimal,
    layers: Sequence[LayerView],
    average_before: Decimal,
    movement_unit_cost: Decimal | None,
    fallback_cost: Decimal,
    carried: Sequence[LayerCost] = (),
) -> Valuation:
    """Cost one movement against the position's FIFO layers. Pure.

    Three shapes, and the third is not an afterthought:

    * **on-hand rises.** One layer is opened at the arrival's price - or several,
      when ``carried`` states an exact breakdown, which is how a stock transfer
      re-opens at its destination precisely what its source gave up. Nothing
      already on the shelf is re-priced. This is the difference from a weighted
      average that a rising-price series makes visible: buy 100 @ 10 then
      100 @ 20 and the position holds two layers, not 200 units at 15.
    * **on-hand falls.** Layers are consumed oldest first and the movement costs
      what they cost. On the same series, issuing 100 costs 1 000 under FIFO and
      1 500 under a weighted average, and the 100 left on the shelf are worth
      2 000 rather than 1 500.
    * **on-hand does not move.** A QC hold, a release, scrap written off at a
      job-work vendor. No layer is opened and none is consumed - the goods have
      not arrived and have not left, they have only changed pool - so the
      position's value is untouched. The ledger row still carries a price,
      because a movement with no cost on it cannot be reported.

    ``magnitude`` is what the ledger row records and ``on_hand_delta`` is what
    the layers follow. They agree in size for every movement that moves on-hand
    (a pool transition changes it by exactly one magnitude, in one direction),
    and they deliberately disagree for the third shape above, where the ledger
    records 20 units held while the layers see nothing.
    """
    value_before = open_layer_value(layers)
    basis = position_cost_basis(layers)

    if on_hand_delta > 0:
        opened = _layers_to_open(
            quantity=to_quantity(on_hand_delta),
            carried=carried,
            unit_cost=inbound_unit_cost(
                movement_unit_cost=movement_unit_cost,
                fallback_cost=fallback_cost,
                position_basis=basis,
                average_before=average_before,
            ),
        )
        movement_value = sum((layer.value for layer in opened), Decimal("0"))
        value_after = value_before + movement_value
        draws: tuple[LayerDraw, ...] = ()
        uncovered = Decimal("0")
        uncovered_unit_cost = Decimal("0")
    elif on_hand_delta < 0:
        consumption = consume_layers(
            layers,
            -on_hand_delta,
            uncovered_unit_cost=outbound_unit_cost(
                position_basis=basis,
                average_before=average_before,
                movement_unit_cost=movement_unit_cost,
                fallback_cost=fallback_cost,
            ),
        )
        opened = ()
        draws = consumption.draws
        uncovered = consumption.uncovered_quantity
        uncovered_unit_cost = consumption.uncovered_unit_cost
        movement_value = consumption.value
        # Only what actually came out of a layer leaves the position's value.
        # Subtracting the uncovered part as well would drive stock_value
        # negative on exactly the position that is already short of layers.
        value_after = value_before - consumption.layer_value
    else:
        opened = ()
        draws = ()
        uncovered = Decimal("0")
        uncovered_unit_cost = Decimal("0")
        value_after = value_before
        movement_value = magnitude * outbound_unit_cost(
            position_basis=basis,
            average_before=average_before,
            movement_unit_cost=movement_unit_cost,
            fallback_cost=fallback_cost,
        )

    stock_value = to_stock_value(value_after)
    return Valuation(
        unit_cost=to_unit_price(movement_value / magnitude) if magnitude else Decimal("0.000000"),
        total_cost=to_money(movement_value),
        average_cost=derive_average_cost(
            stock_value=stock_value, on_hand=on_hand_after, previous_average=average_before
        ),
        stock_value=stock_value,
        opened=opened,
        draws=draws,
        uncovered_quantity=uncovered,
        uncovered_unit_cost=uncovered_unit_cost,
    )


def _layers_to_open(
    *, quantity: Decimal, carried: Sequence[LayerCost], unit_cost: Decimal
) -> tuple[LayerCost, ...]:
    """The layers an arrival opens: one at its price, or the exact costs carried.

    A carried breakdown that falls short of the quantity is topped up at the
    movement's own price rather than refused - a transfer whose outbound leg met
    a position short of layers dispatched real goods, and refusing the arrival
    would strand them in transit with no edge out (the in-transit note above
    :func:`validate_movement`). A breakdown that *exceeds* the quantity is the
    opposite: it would book value the movement never carried, so it is refused.
    """
    supplied = [
        LayerCost(quantity=to_quantity(cost.quantity), unit_cost=to_unit_price(cost.unit_cost))
        for cost in carried
        if to_quantity(cost.quantity) > 0
    ]
    if not supplied:
        return (LayerCost(quantity=quantity, unit_cost=unit_cost),)

    total = sum((cost.quantity for cost in supplied), Decimal("0"))
    if total > quantity:
        raise ValidationError(
            "The carried cost breakdown is larger than the quantity received.",
            details={"carried_quantity": str(total), "quantity": str(quantity)},
        )
    if total < quantity:
        supplied.append(LayerCost(quantity=quantity - total, unit_cost=unit_cost))
    return tuple(supplied)


def draws_on_scrap(movement: MovementRequest) -> bool:
    """Does this movement take its quantity out of the scrap pool?

    Asked of the *resolved* source, like every other pool question in this
    module: a caller can name the source explicitly, and the type alone does
    not say where the quantity came from.
    """
    source, _ = resolve_pools(movement.transaction_type, movement.stock_state, movement.from_state)
    return source is StockState.SCRAP


def scrap_layers_for(valuation: Valuation, magnitude: Decimal) -> tuple[LayerCost, ...]:
    """The scrap cost layers a movement into scrap opens: what its ledger row charged.

    A movement that took the stock out of on-hand charged exactly the layers it
    drew, plus - in a position short of layers - the uncovered remainder at the
    price that was charged for it. One that moved no on-hand at all (scrap
    written off at a job-work vendor, out of a pool that has no layers) charged
    one price for the whole quantity. Either way the scrap queue opens at the
    costs the ledger row states, in the order they were drawn, so the queue a
    disposal is valued from and the ledger cannot disagree. Pure.
    """
    drawn = tuple(
        LayerCost(quantity=draw.quantity, unit_cost=draw.unit_cost) for draw in valuation.draws
    )
    if valuation.uncovered_quantity > 0:
        drawn += (
            LayerCost(
                quantity=valuation.uncovered_quantity,
                unit_cost=valuation.uncovered_unit_cost,
            ),
        )
    if drawn:
        return drawn
    return (LayerCost(quantity=to_quantity(magnitude), unit_cost=valuation.unit_cost),)


@dataclass(frozen=True, slots=True)
class LineCost:
    """What one document line will cost when it posts.

    ``total_cost`` is at money scale, not stock-value scale: it is a *document*
    figure - what the line shows, what the header adds up, and what an approval
    threshold is measured against - and a document total is presented at two
    places. The ledger row the line becomes is rounded the same way, so the two
    agree to the paisa.
    """

    unit_cost: Decimal
    total_cost: Decimal


class FifoCostPreview:
    """What a document's lines will cost, priced off the layers they will consume.

    A document is drafted before it posts and has to say what it is worth. Under
    a moving average that number was free: every unit of a position cost the
    same, so ``quantity x average_cost`` *was* what the ledger would book. Under
    FIFO it is not, and reading ``average_cost`` gave a stock adjustment a value
    that had nothing to do with what the engine would charge - a write-off of
    60 units from a position holding 100 @ 500 and 900 @ 10 showed 3 540 and
    posted 30 000, which walked straight through a 25 000 approval threshold.

    So a draft is costed the way the posting will be: the position's open layers,
    oldest first, exactly as :func:`consume_layers` will take them. Including the
    repair :meth:`MovementEngine._reconcile_layers` would apply first, so the
    preview and the posting start from the same queue rather than from two
    slightly different ones.

    Deliberately **not** a lock, and deliberately not authoritative:

    * it reads the layers without ``FOR UPDATE``, because a draft must not hold
      a row lock across a user's thinking time;
    * a position can therefore move between the draft and the posting, which is
      exactly why :meth:`StockAdjustmentService.post` re-checks the approval
      threshold against the cost the ledger *actually* booked and refuses a
      document that turns out to need an approval it never got.

    Lines are costed in the order they are given, and each one spends the queue
    it drew from, so two lines against the same position consume in sequence
    instead of both pricing themselves off the same oldest layer.
    """

    def __init__(
        self,
        positions: dict[BalanceGrain, InventoryBalance],
        layers: dict[uuid.UUID, list[InventoryCostLayer]],
    ) -> None:
        self._positions = positions
        self._layers = layers
        self._queues: dict[BalanceGrain, list[LayerView]] = {}

    @classmethod
    async def load(
        cls,
        inventory: InventoryRepository,
        cost_layers: CostLayerRepository,
        grains: Sequence[BalanceGrain],
    ) -> FifoCostPreview:
        """Two queries for a whole document: the positions, then their layers."""
        positions = await inventory.positions_for(grains)
        layers = await cost_layers.open_layers_for_balances(
            [balance.id for balance in positions.values()]
        )
        return cls(positions, layers)

    def on_hand(self, grain: BalanceGrain) -> Decimal:
        position = self._positions.get(grain)
        return Decimal(position.on_hand_quantity) if position is not None else Decimal("0")

    def line_cost(
        self,
        grain: BalanceGrain,
        quantity: Decimal,
        *,
        supplied: Decimal | None,
        fallback_cost: Decimal,
    ) -> LineCost:
        """Cost one line, and spend what it takes out of the preview queue.

        A line that *adds* stock is priced by :func:`inbound_unit_cost`, which is
        the same precedence the engine applies to the arrival - so the price the
        document states is the price the layer opens at, and the two cannot
        disagree. A line that *removes* stock is costed against the layers.
        """
        magnitude = abs(to_quantity(quantity))
        if magnitude == 0:
            return LineCost(unit_cost=Decimal("0.000000"), total_cost=Decimal("0.00"))

        queue = self._queue(grain, fallback_cost)
        basis = position_cost_basis(queue)
        average = self._average(grain)

        if quantity > 0:
            unit_cost = inbound_unit_cost(
                movement_unit_cost=supplied,
                fallback_cost=fallback_cost,
                position_basis=basis,
                average_before=average,
            )
            # The arrival joins the queue behind everything already standing at
            # the position, so a later line on the same grain draws the old
            # stock first - which is what the engine will do with it.
            queue.append(LayerView(remaining=magnitude, unit_cost=unit_cost))
            return LineCost(unit_cost=unit_cost, total_cost=to_money(magnitude * unit_cost))

        consumption = consume_layers(
            queue,
            magnitude,
            uncovered_unit_cost=outbound_unit_cost(
                position_basis=basis,
                average_before=average,
                movement_unit_cost=supplied,
                fallback_cost=fallback_cost,
            ),
        )
        self._spend(queue, consumption)
        return LineCost(
            unit_cost=to_unit_price(consumption.value / magnitude),
            total_cost=to_money(consumption.value),
        )

    def _average(self, grain: BalanceGrain) -> Decimal:
        position = self._positions.get(grain)
        return Decimal(position.average_cost) if position is not None else Decimal("0")

    def _queue(self, grain: BalanceGrain, fallback_cost: Decimal) -> list[LayerView]:
        """The position's FIFO queue as the posting will find it.

        Built once per grain and then spent, and reconciled on the way in the
        same two directions :meth:`MovementEngine._reconcile_layers` reconciles:
        a preview that priced a shortfall at nothing, or an excess at more than
        the position holds, would state a cost the posting is guaranteed not to
        charge.
        """
        queue = self._queues.get(grain)
        if queue is not None:
            return queue

        position = self._positions.get(grain)
        views: list[LayerView] = []
        if position is not None:
            views = [
                LayerView(
                    remaining=Decimal(layer.remaining_quantity),
                    unit_cost=Decimal(layer.unit_cost),
                )
                for layer in self._layers.get(position.id, ())
            ]
            on_hand = Decimal(position.on_hand_quantity)
            covered = open_layer_quantity(views)
            if covered < on_hand:
                views.insert(
                    0,
                    LayerView(
                        remaining=to_quantity(on_hand - covered),
                        unit_cost=_first_positive(Decimal(position.average_cost), fallback_cost),
                    ),
                )
            elif covered > on_hand:
                self._spend(
                    views,
                    consume_layers(
                        views,
                        to_quantity(covered - on_hand),
                        uncovered_unit_cost=Decimal("0"),
                    ),
                )
        self._queues[grain] = views
        return views

    @staticmethod
    def _spend(queue: list[LayerView], consumption: LayerConsumption) -> None:
        for draw in consumption.draws:
            layer = queue[draw.index]
            queue[draw.index] = LayerView(
                remaining=layer.remaining - draw.quantity, unit_cost=layer.unit_cost
            )
        queue[:] = [layer for layer in queue if layer.remaining > 0]


@dataclass(frozen=True, slots=True)
class StockShortfall:
    """Why a movement was refused, and what there was to draw on."""

    rule: str
    available: Decimal


def find_shortfall(
    *,
    before: dict[str, Decimal],
    after: dict[str, Decimal],
    reserved: Decimal,
    allow_negative_available: bool,
) -> StockShortfall | None:
    """Check a proposed balance against every rule the database will enforce.

    Run before the UPDATE so a shortage comes back as a typed business error
    naming the item and the quantity, rather than as an opaque integrity error
    from a check constraint. The constraints are still there and still win - this
    is the friendly half of the same rule, never a replacement for it.

    Order matters. Negative on-hand and unbacked reservations are checked first
    because no warehouse setting may relax them; the availability check is last
    because ``allow_negative_stock`` legitimately turns it off for line-side WIP.
    """
    if after["on_hand_quantity"] < 0:
        return StockShortfall("on_hand", before["on_hand_quantity"])

    if reserved > after["on_hand_quantity"]:
        return StockShortfall("reserved", before["on_hand_quantity"] - reserved)

    for column in _POOL_COLUMNS:
        if after[column] < 0:
            return StockShortfall(column, before[column])

    available_after = (
        after["on_hand_quantity"] - reserved - after["qc_hold_quantity"] - after["damaged_quantity"]
    )
    if available_after < 0 and not allow_negative_available:
        available_before = (
            before["on_hand_quantity"]
            - reserved
            - before["qc_hold_quantity"]
            - before["damaged_quantity"]
        )
        return StockShortfall("available", available_before)

    return None


# ASSUMPTION: master-data policy gates the *start* of a movement into a
# position, never the *completion* of one that is already under way, and never
# the *exit* of a quantity the position already holds.
#
# The exit half is the one this rule kept losing. Whichever flag is at stake -
# inactive item, retired bin, batch tracking, serial tracking - a rule that can
# refuse an outbound movement does not make the stock conform to the flag. It
# makes the stock unreachable, which is the same failure as the in-transit one
# below with a different document on the front of it. So every policy rule in
# :func:`validate_movement` is gated on ``gates_policy``, and ``gates_policy``
# is computed from the movement's *resolved source pool* - see the note there.
#
# The invariant a stock transfer needs is not "dispatch is strict" or "receive is
# lenient". It is: **anything dispatch lets through, receive accepts.** Between
# the two the goods are on a vehicle, off the source's on-hand and standing in
# the destination's in_transit_quantity, and ``TRANSFER_FLOW`` has no edge out of
# IN_TRANSIT except RECEIVED. Any rule that can refuse the inbound leg after the
# outbound leg has posted does not protect anything - it strands the stock where
# nobody can reach it, on a document nobody can close.
#
# Two halves, and both are needed:
#
# 1. ``dispatch`` validates the destination with exactly the rules ``receive``
#    will apply to it (:meth:`MovementEngine._validate_grain`, ``receiving=True``
#    - the same three active-flag rules :func:`validate_movement` applies to an
#    inbound movement). Refusing at dispatch is safe: nothing has moved, and the
#    document stays APPROVED, which is cancellable.
# 2. Master data can still change *while* the goods travel, and no amount of
#    validation at dispatch can see the future. So the arrival is held only to
#    the rules about the position being real - the row exists, it is this
#    tenant's, the bin is in the warehouse, the batch and unit belong to the item
#    - and not to policy that changed after the goods left. Deactivating an item,
#    a warehouse or a bin, or turning batch tracking on, must not be able to
#    strand a lorry-load.
#
# The alternative - refuse the arrival - keeps the quantity in
# ``in_transit_quantity`` for good: on the balance sheet, absent from on-hand,
# and unreachable by any document. That is strictly worse than booking goods
# that are physically standing there into a position that is retired, which the
# operator can then transfer out or write off.
#: Movements that consume stock rather than shuffle custody. An expired or
#: quarantined lot may still be transferred, returned to a vendor, adjusted or
#: disposed of - refusing those would trap it - but it may not be issued,
#: dispatched, consumed by assembly or sent to a job-work vendor.
BATCH_ISSUE_BLOCKED: frozenset[TransactionType] = frozenset(
    {
        TransactionType.MATERIAL_ISSUE,
        TransactionType.DISPATCH,
        TransactionType.ASSEMBLY_CONSUME,
        TransactionType.JOB_WORK_ISSUE,
    }
)

#: Movements where the engine picks a lot when the caller named none. Custody
#: moves and adjustments keep their explicit lot: a vendor return or a write-off
#: must say which lot is leaving, while an ordinary issue is exactly the case
#: FEFO exists to decide.
FEFO_MOVEMENTS: frozenset[TransactionType] = frozenset(
    {
        TransactionType.MATERIAL_ISSUE,
        TransactionType.DISPATCH,
        TransactionType.ASSEMBLY_CONSUME,
        TransactionType.JOB_WORK_ISSUE,
        TransactionType.TRANSFER_OUT,
    }
)

#: How far ahead the expiry sweep warns. Thirty days is long enough to use a
#: lot up or arrange a return, short enough that the warning still means
#: something when it arrives.
BATCH_EXPIRY_WARN_DAYS = 30


def validate_movement(
    movement: MovementRequest,
    *,
    item: ItemFacts,
    warehouse: WarehouseFacts,
    batch_owner: tuple[uuid.UUID, str] | None,
    serial_owner: tuple[uuid.UUID, str] | None,
    location: LocationFacts | None = None,
    batch_status: BatchStatus | None = None,
    batch_expires_on: date | None = None,
) -> None:
    """Master-data rules a movement must satisfy, independent of any balance.

    Pure: everything it needs has already been fetched, so the rules can be
    exercised without a database and no query can creep into the write path.

    Two classes of rule live here and the difference decides which of them a
    movement completing an in-transit quantity is still held to - see the
    in-transit note above:

    * **Position integrity** - the bin is in the warehouse the movement names,
      the batch and the unit belong to this item, a serial movement is one unit.
      Always enforced. Breaking one of these loses the stock.
    * **Master-data policy** - the active flags, and whether a batch or a serial
      is required. These gate *starting* stock into a position, and nothing
      else: a movement that draws on a pool the position already holds is not
      held to any of them, or the flag would trap the stock it was flipped
      over. The one movement that finishes a journey the company has already
      committed to and already validated is exempt as well.
    """
    # Where this movement takes its quantity *from*. ``None`` is the whole
    # definition of "starts stock into this position": a purchase receipt, a
    # production output, the inbound leg of a transfer. Anything else - an
    # issue, a QC release out of the hold pool, a job-work return out of the
    # job-work pool - is moving a quantity that is already at this position, and
    # master-data policy has nothing left to gate.
    #
    # Taken from the *resolved* pools rather than from the type's sign, because
    # the sign is a property of the type alone and the caller can override the
    # source: ``change_stock_state(JOB_WORK -> AVAILABLE)`` posts a
    # ``JOB_WORK_RECEIPT``, which is a +1 type, but it draws on the job-work
    # pool of this very position. Gating it on the sign made a policy flag able
    # to trap that pool - the same defect one line down, in a different bucket.
    source_state, target_state = resolve_pools(
        movement.transaction_type, movement.stock_state, movement.from_state
    )
    # ASSUMPTION: ``items.can_scrap`` gates every movement that *lands* stock in
    # the scrap pool - a SCRAP transaction, a QC scrap verdict, a job-work
    # vendor's scrap - and nothing else (docs/assumptions.md §E8-5). It is a
    # rule about where stock may go, so refusing it strands nothing: the
    # quantity stays in the pool it was in, and the QC sheet offers DAMAGED
    # instead. Disposing of scrap the item already holds stays open - the same
    # "a flag never traps stock it was flipped over" rule as every other flag.
    if target_state is StockState.SCRAP and not item.can_scrap:
        raise BusinessRuleError(
            f"Item {item.code} cannot be scrapped; its item master does not allow it. "
            "Hold the quantity as damaged instead.",
            details={"item_id": str(item.id), "item_code": item.code},
        )
    # Scrap leaves stock one way only. A movement that drew on the scrap pool and
    # landed somewhere else would bring written-off stock back into a usable
    # pool without the write-up and reason that changes the balance sheet -
    # ``state_change_type`` refuses the same thing for ``change_stock_state``;
    # this closes it for a hand-built request as well.
    if source_state is StockState.SCRAP and target_state is not None:
        raise BusinessRuleError(
            "Scrap leaves stock only through a scrap disposal. Bringing it back into "
            "stock is a write-up with a reason - post a stock adjustment instead.",
            details={"item_code": item.code, "to_state": str(target_state)},
        )
    # A policy rule gates a movement that brings stock into a position. The
    # arrival of goods that are already in transit is not such a movement: the
    # decision was taken, and checked, at dispatch.
    gates_policy = source_state is None and not movement.completes_in_transit

    # ASSUMPTION: a deactivated item or warehouse may still give stock up, but
    # may not take any in. Refusing outbound movements too would strand existing
    # stock behind a flag, which is worse than the thing the flag prevents.
    if gates_policy and not item.is_active:
        raise BusinessRuleError(
            inactive_record("Item", item.code, "cannot receive stock"),
            details={"item_code": item.code},
        )
    if gates_policy and not warehouse.is_active:
        raise BusinessRuleError(
            inactive_record("Warehouse", warehouse.code, "cannot receive stock"),
            details={"warehouse_code": warehouse.code},
        )

    if location is not None:
        # The balance grain is (company, item, warehouse, location, batch). A bin
        # from a different warehouse makes that tuple describe a place that does
        # not exist, and the position it creates is invisible to every warehouse
        # view - which is worse than an error, because nothing ever reports it.
        if location.warehouse_id != movement.warehouse_id:
            raise BusinessRuleError(
                f"Storage location {location.code} is not in warehouse {warehouse.code}.",
                details={
                    "location_id": str(movement.location_id),
                    "location_code": location.code,
                    "warehouse_code": warehouse.code,
                    "location_warehouse_id": str(location.warehouse_id),
                },
            )
        # ASSUMPTION: a retired bin may still give stock up, but may not take any
        # in - the same rule the inactive item and warehouse above already
        # follow, for the same reason. Refusing outbound movements would strand
        # whatever is still in the bin behind the flag that retired it.
        if gates_policy and not location.is_active:
            raise BusinessRuleError(
                inactive_record("Storage location", location.code, "cannot receive stock"),
                details={
                    "location_id": str(movement.location_id),
                    "location_code": location.code,
                    "warehouse_code": warehouse.code,
                },
            )

    # Traceability is refused up front rather than patched afterwards: a
    # movement posted without its batch cannot be attributed to a lot later, and
    # a recall then has to quarantine everything.
    #
    # ASSUMPTION: and it is a rule about what you may **put into** a position,
    # not about what may leave one. A batch or a serial is assigned when goods
    # arrive; stock already sitting at a batch-NULL grain is a historical fact,
    # and refusing to let it out does not make it batch tracked - it makes it
    # unreachable. ``gates_policy`` is therefore the gate here too, exactly as
    # it is for the three active flags above, and for the same reason.
    #
    # Ungated, this rule was direction-blind and closed both routes out of such
    # a position at once: a movement naming no batch was refused by the rule,
    # and a movement naming one keys a *different* ``BalanceGrain`` (``batch_id``
    # is part of the grain) which holds nothing. Four units that arrived on a
    # transfer while the flag was flipped were owned, on the balance sheet, and
    # immovable for good.
    #
    # Outbound is safe to leave open because the grain does the limiting: a
    # batch-less issue can only ever draw on the batch-less position, whose
    # quantity is precisely the stock that predates the flag. It cannot reach a
    # lot, and ``on_hand_quantity`` may not go negative under any warehouse
    # setting, so it cannot invent one either.
    #
    # ``gates_policy`` also carries the in-transit exemption: an item that
    # became batch or serial tracked while goods were on a vehicle. The quantity
    # left its source without a lot - the flag did not exist yet - so refusing
    # its arrival creates no traceability at all, it only leaves the stock
    # nowhere. The document that dispatched it is the trace, and it is on every
    # ledger row of both legs; ``MovementEngine._audit`` additionally records the
    # gap on the movement so the untraced quantity can be found afterwards
    # rather than discovered during a recall.
    if gates_policy and item.is_batch_tracked and movement.batch_id is None:
        raise BusinessRuleError(
            f"Item {item.code} is batch tracked; a batch is required to receive it into stock.",
            code=ErrorCode.BATCH_REQUIRED,
            details={"item_code": item.code, "item_id": str(item.id)},
        )
    # The serial rule is NOT gated on ``gates_policy``, and the difference from
    # the batch rule immediately above is the whole point.
    #
    # The batch rule can be left open outbound because the grain does the
    # limiting: ``batch_id`` is part of ``BalanceGrain``, so a batch-less issue
    # can only ever reach the batch-less position - precisely the stock that
    # predates the flag.
    #
    # ``serial_id`` is NOT part of the grain. A serial-less departure therefore
    # draws on the *whole* position, identified units included, and the units it
    # takes are chosen by nobody: stock leaves while every ``stock_serials`` row
    # still says IN_STOCK. That is exactly the defect the serial lifecycle
    # exists to prevent - a unit reported on hand after it has physically
    # shipped, and issuable a second time. Gating this rule by direction
    # reintroduces it.
    #
    # The gate is therefore ``SERIAL_MOVEMENT_TARGETS`` - the same mapping that
    # decides the unit's next status - so this rule and the lifecycle cannot
    # drift apart: exactly the movements that change whose hands hold the unit
    # are the ones that must name it. A movement that leaves the status alone (a
    # QC hold or release, or disposal out of the scrap pool) moves a quantity
    # rather than a unit, so it stays ungated and cannot trap a pool. Both legs
    # of a transfer now change status - IN_TRANSIT, then IN_STOCK - and are
    # gated like any other movement of a known unit.
    #
    # This cannot strand stock the way the ungated batch rule did, because the
    # situation is closed at source: ``products._assert_tracking_change_is_safe``
    # refuses to switch serial tracking on while the item holds anything. The
    # in-transit exemption is still honoured - goods that left before the flag
    # existed have no unit to name, and refusing their arrival would leave them
    # nowhere rather than create traceability.
    serial_gates = (
        gates_policy or SERIAL_MOVEMENT_TARGETS.get(movement.transaction_type) is not None
    ) and not movement.completes_in_transit
    if serial_gates and item.is_serial_tracked and movement.serial_id is None:
        raise BusinessRuleError(
            f"Item {item.code} is serial tracked; a serial number is required to move it.",
            code=ErrorCode.SERIAL_REQUIRED,
            details={"item_code": item.code, "item_id": str(item.id)},
        )

    if batch_owner is not None and batch_owner[0] != movement.item_id:
        raise BusinessRuleError(
            f"Batch {batch_owner[1]} belongs to a different item.",
            details={"batch_id": str(movement.batch_id), "item_code": item.code},
        )

    if batch_owner is not None and batch_status is not None:
        # Inbound policy. A transfer completing from IN_TRANSIT is exempt for
        # the same reason it is exempt from the tracking flags: the decision was
        # taken at dispatch, and refusing the arrival would strand stock in a
        # pool nobody can reach.
        if gates_policy and batch_status is not BatchStatus.ACTIVE:
            raise BusinessRuleError(
                f"Batch {batch_owner[1]} is {batch_status} and cannot receive stock.",
                details={
                    "batch_id": str(movement.batch_id),
                    "batch_number": batch_owner[1],
                    "status": str(batch_status),
                },
            )
        expires_on = batch_expires_on
        if gates_policy and expires_on is not None and expires_on < today():
            raise BusinessRuleError(
                f"Batch {batch_owner[1]} expired on {expires_on.isoformat()} and cannot "
                "receive stock.",
                details={
                    "batch_id": str(movement.batch_id),
                    "batch_number": batch_owner[1],
                    "expires_on": expires_on.isoformat(),
                },
            )
        # Outbound policy. Expiry is checked from the date as well as the status:
        # a batch whose expiry has passed but whose sweep has not run yet is
        # just as unusable.
        if movement.transaction_type in BATCH_ISSUE_BLOCKED and (
            batch_status is not BatchStatus.ACTIVE
            or (expires_on is not None and expires_on < today())
        ):
            raise BusinessRuleError(
                f"Batch {batch_owner[1]} is expired or quarantined and cannot be issued.",
                details={
                    "batch_id": str(movement.batch_id),
                    "batch_number": batch_owner[1],
                    "status": str(batch_status),
                    "expires_on": expires_on.isoformat() if expires_on else None,
                },
            )

    if serial_owner is not None:
        if serial_owner[0] != movement.item_id:
            raise BusinessRuleError(
                f"Serial {serial_owner[1]} belongs to a different item.",
                details={"serial_id": str(movement.serial_id), "item_code": item.code},
            )
        # A serial identifies exactly one physical unit, so a movement that
        # names one can only ever be for one.
        if abs(to_quantity(Decimal(movement.quantity))) != Decimal("1"):
            raise BusinessRuleError(
                f"Serial {serial_owner[1]} identifies a single unit; the movement quantity "
                "must be 1.",
                details={"serial_number": serial_owner[1], "quantity": str(movement.quantity)},
            )


# -----------------------------------------------------------------------------
# Document state machines (section 73)
# -----------------------------------------------------------------------------

TRANSFER_FLOW: StateMachine[TransferStatus] = StateMachine(
    entity="Stock transfer",
    initial=TransferStatus.DRAFT,
    transitions=(
        Transition(
            TransferStatus.DRAFT,
            TransferStatus.APPROVED,
            permission=Permission.INVENTORY_TRANSFER,
            label="Approve",
        ),
        Transition(
            TransferStatus.APPROVED,
            TransferStatus.IN_TRANSIT,
            permission=Permission.INVENTORY_TRANSFER,
            label="Dispatch",
            is_irreversible=True,
        ),
        Transition(
            TransferStatus.IN_TRANSIT,
            TransferStatus.RECEIVED,
            permission=Permission.INVENTORY_TRANSFER,
            label="Receive",
            is_irreversible=True,
        ),
        # No edge out of IN_TRANSIT other than RECEIVED: the stock is physically
        # on a vehicle. Cancelling would leave it owned by nobody.
        Transition(
            TransferStatus.DRAFT,
            TransferStatus.CANCELLED,
            permission=Permission.INVENTORY_TRANSFER,
            label="Cancel",
        ),
        Transition(
            TransferStatus.APPROVED,
            TransferStatus.CANCELLED,
            permission=Permission.INVENTORY_TRANSFER,
            label="Cancel",
        ),
    ),
    terminal=frozenset({TransferStatus.RECEIVED, TransferStatus.CANCELLED}),
)

ADJUSTMENT_FLOW: StateMachine[AdjustmentStatus] = StateMachine(
    entity="Stock adjustment",
    initial=AdjustmentStatus.DRAFT,
    transitions=(
        # Below the approval threshold a draft posts directly; above it the
        # service refuses this edge and demands the approval route.
        Transition(
            AdjustmentStatus.DRAFT,
            AdjustmentStatus.POSTED,
            permission=Permission.INVENTORY_ADJUST,
            label="Post",
            is_irreversible=True,
        ),
        Transition(
            AdjustmentStatus.DRAFT,
            AdjustmentStatus.PENDING_APPROVAL,
            permission=Permission.INVENTORY_ADJUST,
            label="Submit for approval",
        ),
        Transition(
            AdjustmentStatus.PENDING_APPROVAL,
            AdjustmentStatus.APPROVED,
            permission=Permission.INVENTORY_ADJUST_APPROVE,
            label="Approve",
        ),
        Transition(
            AdjustmentStatus.PENDING_APPROVAL,
            AdjustmentStatus.REJECTED,
            permission=Permission.INVENTORY_ADJUST_APPROVE,
            label="Reject",
        ),
        Transition(
            AdjustmentStatus.APPROVED,
            AdjustmentStatus.POSTED,
            permission=Permission.INVENTORY_ADJUST,
            label="Post",
            is_irreversible=True,
        ),
        Transition(
            AdjustmentStatus.DRAFT,
            AdjustmentStatus.CANCELLED,
            permission=Permission.INVENTORY_ADJUST,
            label="Cancel",
        ),
        Transition(
            AdjustmentStatus.PENDING_APPROVAL,
            AdjustmentStatus.CANCELLED,
            permission=Permission.INVENTORY_ADJUST,
            label="Cancel",
        ),
        Transition(
            AdjustmentStatus.APPROVED,
            AdjustmentStatus.CANCELLED,
            permission=Permission.INVENTORY_ADJUST,
            label="Cancel",
        ),
    ),
    terminal=frozenset(
        {AdjustmentStatus.POSTED, AdjustmentStatus.REJECTED, AdjustmentStatus.CANCELLED}
    ),
)

COUNT_FLOW: StateMachine[PhysicalCountStatus] = StateMachine(
    entity="Physical count",
    initial=PhysicalCountStatus.DRAFT,
    transitions=(
        Transition(
            PhysicalCountStatus.DRAFT,
            PhysicalCountStatus.IN_PROGRESS,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Start counting",
        ),
        Transition(
            PhysicalCountStatus.IN_PROGRESS,
            PhysicalCountStatus.COUNTED,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Finish counting",
        ),
        Transition(
            PhysicalCountStatus.COUNTED,
            PhysicalCountStatus.CONFIRMED,
            permission=Permission.INVENTORY_RECONCILE,
            label="Confirm and post variance",
            is_irreversible=True,
        ),
        # Re-open a finished sheet for a recount, as long as it is not posted.
        Transition(
            PhysicalCountStatus.COUNTED,
            PhysicalCountStatus.IN_PROGRESS,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Recount",
        ),
        Transition(
            PhysicalCountStatus.DRAFT,
            PhysicalCountStatus.CANCELLED,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Cancel",
        ),
        Transition(
            PhysicalCountStatus.IN_PROGRESS,
            PhysicalCountStatus.CANCELLED,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Cancel",
        ),
        Transition(
            PhysicalCountStatus.COUNTED,
            PhysicalCountStatus.CANCELLED,
            permission=Permission.INVENTORY_PHYSICAL_COUNT,
            label="Cancel",
        ),
    ),
    terminal=frozenset({PhysicalCountStatus.CONFIRMED, PhysicalCountStatus.CANCELLED}),
)

#: A bin move is raised and posted in one request, so it has one edge. The
#: machine exists anyway because a status column changes through a machine
#: everywhere in this system; the transition is audited like any other.
BIN_MOVE_FLOW: StateMachine[BinMoveStatus] = StateMachine(
    entity="Bin move",
    initial=BinMoveStatus.DRAFT,
    transitions=(
        Transition(
            BinMoveStatus.DRAFT,
            BinMoveStatus.POSTED,
            permission=Permission.INVENTORY_TRANSFER,
            label="Post",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({BinMoveStatus.POSTED}),
)

#: The same single edge as a bin move, for the same reason.
SCRAP_DISPOSAL_FLOW: StateMachine[ScrapDisposalStatus] = StateMachine(
    entity="Scrap disposal",
    initial=ScrapDisposalStatus.DRAFT,
    transitions=(
        Transition(
            ScrapDisposalStatus.DRAFT,
            ScrapDisposalStatus.POSTED,
            permission=Permission.INVENTORY_SCRAP_DISPOSE,
            label="Post",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({ScrapDisposalStatus.POSTED}),
)

#: DRAFT -> POSTED below the approval value; DRAFT -> PENDING_APPROVAL ->
#: POSTED above it, approving and posting being one step. CANCELLED from either
#: open status - by whoever raises issues (Cancel) or by an approver refusing a
#: pending one (Reject), both recorded with their reason. The approver's edge
#: carries ``internal_issue.approve``; the route is what enforces it.
INTERNAL_ISSUE_FLOW: StateMachine[InternalIssueStatus] = StateMachine(
    entity="Internal issue",
    initial=InternalIssueStatus.DRAFT,
    transitions=(
        Transition(
            InternalIssueStatus.DRAFT,
            InternalIssueStatus.POSTED,
            permission=Permission.INTERNAL_ISSUE_CREATE,
            label="Post",
            is_irreversible=True,
        ),
        Transition(
            InternalIssueStatus.DRAFT,
            InternalIssueStatus.PENDING_APPROVAL,
            permission=Permission.INTERNAL_ISSUE_CREATE,
            label="Submit for approval",
        ),
        Transition(
            InternalIssueStatus.PENDING_APPROVAL,
            InternalIssueStatus.POSTED,
            permission=Permission.INTERNAL_ISSUE_APPROVE,
            label="Approve and post",
            is_irreversible=True,
        ),
        Transition(
            InternalIssueStatus.DRAFT,
            InternalIssueStatus.CANCELLED,
            permission=Permission.INTERNAL_ISSUE_CREATE,
            label="Cancel",
        ),
        Transition(
            InternalIssueStatus.PENDING_APPROVAL,
            InternalIssueStatus.CANCELLED,
            permission=Permission.INTERNAL_ISSUE_CREATE,
            label="Cancel",
        ),
    ),
    terminal=frozenset({InternalIssueStatus.POSTED, InternalIssueStatus.CANCELLED}),
)


# -----------------------------------------------------------------------------
# Serial lifecycle
# -----------------------------------------------------------------------------

#: The lifecycle of one individually identified unit.
#:
#: A serial is not a document: nobody presses a button to move it from
#: ``IN_STOCK`` to ``ISSUED``. It advances as a side effect of the movement that
#: names it, which is why no edge carries a permission - the authorisation for
#: the transition is the authorisation for the goods issue, the dispatch or the
#: adjustment that posts it, and that is checked on the document's own route.
#:
#: The self edge on ``IN_STOCK`` is deliberate. A serial row is registered before
#: the goods arrive - the number is keyed off the packing list - and it is
#: registered ``IN_STOCK``, because the vocabulary has no "expected" state. The
#: receipt that follows would otherwise be an illegal transition from the unit's
#: own starting state. See ``docs/assumptions.md``.
#:
#: The edge is *not* a licence to receive the same unit twice: it is qualified by
#: ``already_received`` in :func:`serial_transition`, which the engine answers
#: from the ledger. Without that qualification the graph guarded only the
#: outbound half of the lifecycle - a re-scanned goods receipt booked a second
#: unit's worth of stock against one physical object and the unit stayed
#: cheerfully ``IN_STOCK``.
SERIAL_FLOW: StateMachine[SerialStatus] = StateMachine(
    entity="Stock serial",
    initial=SerialStatus.IN_STOCK,
    transitions=(
        Transition(
            SerialStatus.IN_STOCK,
            SerialStatus.IN_STOCK,
            label="Receive",
        ),
        Transition(
            SerialStatus.IN_STOCK,
            SerialStatus.ISSUED,
            label="Issue",
        ),
        Transition(
            SerialStatus.IN_STOCK,
            SerialStatus.DISPATCHED,
            label="Dispatch",
        ),
        Transition(
            SerialStatus.IN_STOCK,
            SerialStatus.SCRAPPED,
            label="Scrap",
            is_irreversible=True,
        ),
        # The outbound leg keeps ownership but takes the unit off the shelf; it
        # is neither issueable at the source nor receivable at the destination
        # until the inbound leg completes.
        Transition(
            SerialStatus.IN_STOCK,
            SerialStatus.IN_TRANSIT,
            label="Transfer out",
        ),
        Transition(
            SerialStatus.IN_TRANSIT,
            SerialStatus.IN_STOCK,
            label="Transfer in",
        ),
        # Material returned from the shop floor. The unit is back on the shelf.
        Transition(
            SerialStatus.ISSUED,
            SerialStatus.IN_STOCK,
            label="Return to store",
        ),
        Transition(
            SerialStatus.ISSUED,
            SerialStatus.SCRAPPED,
            label="Vendor process loss",
            is_irreversible=True,
        ),
        # The only way back from a customer.
        Transition(
            SerialStatus.DISPATCHED,
            SerialStatus.RETURNED,
            label="Customer return",
        ),
        # A returned unit is physically here, so it can be put back into normal
        # stock, sent out again after refurbishment, or written off.
        Transition(
            SerialStatus.RETURNED,
            SerialStatus.IN_STOCK,
            label="Return to stock",
        ),
        Transition(
            SerialStatus.RETURNED,
            SerialStatus.ISSUED,
            label="Issue",
        ),
        Transition(
            SerialStatus.RETURNED,
            SerialStatus.DISPATCHED,
            label="Dispatch",
        ),
        Transition(
            SerialStatus.RETURNED,
            SerialStatus.SCRAPPED,
            label="Scrap",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({SerialStatus.SCRAPPED}),
)

#: A lot's quality lifecycle. CONSUMED is reached automatically and is terminal;
#: an expired lot can still be consumed to zero but cannot be reactivated,
#: because its expiry date is a physical fact rather than a quality decision.
BATCH_FLOW: StateMachine[BatchStatus] = StateMachine(
    entity="Stock batch",
    initial=BatchStatus.ACTIVE,
    transitions=(
        Transition(BatchStatus.ACTIVE, BatchStatus.QUARANTINED, label="Quarantine"),
        Transition(BatchStatus.QUARANTINED, BatchStatus.ACTIVE, label="Reactivate"),
        Transition(
            BatchStatus.ACTIVE,
            BatchStatus.EXPIRED,
            label="Expire",
            is_irreversible=True,
        ),
        Transition(BatchStatus.QUARANTINED, BatchStatus.EXPIRED, label="Expire"),
        Transition(BatchStatus.ACTIVE, BatchStatus.CONSUMED, label="Consume"),
        Transition(BatchStatus.QUARANTINED, BatchStatus.CONSUMED, label="Consume"),
        Transition(BatchStatus.EXPIRED, BatchStatus.CONSUMED, label="Consume"),
    ),
    terminal=frozenset({BatchStatus.CONSUMED}),
)

#: :data:`~src.modules.inventory.models.SERIAL_IN_WAREHOUSE_STATUSES` is
#: re-exported here because it is half of this graph: a movement that handles a
#: unit without changing whose hands it is in - a QC hold or a QC release -
#: still requires the unit to be in a warehouse. A transfer changes the unit's
#: status on both legs: IN_TRANSIT at dispatch, IN_STOCK at receipt.

# ASSUMPTION: which serial status each movement type leaves the unit in. The
# contract fixes the vocabulary of both enums but not the mapping between them.
# Three of these are judgement calls and are named here rather than buried:
# a purchase return sends the unit back to the vendor, which is DISPATCHED
# because the vocabulary has no vendor-return state and the unit has genuinely
# left the company; an adjustment decrease writes a unit off, which is SCRAPPED
# because there is no "lost" state and leaving it IN_STOCK is the defect this
# mapping exists to fix; and a customer return lands on RETURNED rather than
# IN_STOCK, so a unit that has been out to a customer is distinguishable from
# one that never left.
#
#: ``None`` means the movement does not change whose hands the unit is in - it
#: moves between pools of the same position - so the status is left alone and
#: only the quantity at that position is updated. A warehouse transfer is not in
#: this group: its outbound leg parks the unit IN_TRANSIT and its inbound leg
#: lands it IN_STOCK at the destination.
SERIAL_MOVEMENT_TARGETS: dict[TransactionType, SerialStatus | None] = {
    # --- back into our own stock -------------------------------------------
    TransactionType.PURCHASE_RECEIPT: SerialStatus.IN_STOCK,
    TransactionType.PRODUCTION_RECEIPT: SerialStatus.IN_STOCK,
    TransactionType.ASSEMBLY_RECEIPT: SerialStatus.IN_STOCK,
    TransactionType.JOB_WORK_RECEIPT: SerialStatus.IN_STOCK,
    TransactionType.MATERIAL_RETURN: SerialStatus.IN_STOCK,
    TransactionType.OPENING_BALANCE: SerialStatus.IN_STOCK,
    TransactionType.ADJUSTMENT_INCREASE: SerialStatus.IN_STOCK,
    TransactionType.CUSTOMER_RETURN: SerialStatus.RETURNED,
    # --- out of our stock ---------------------------------------------------
    TransactionType.MATERIAL_ISSUE: SerialStatus.ISSUED,
    TransactionType.ASSEMBLY_CONSUME: SerialStatus.ISSUED,
    TransactionType.JOB_WORK_ISSUE: SerialStatus.ISSUED,
    TransactionType.DISPATCH: SerialStatus.DISPATCHED,
    TransactionType.PURCHASE_RETURN: SerialStatus.DISPATCHED,
    TransactionType.SCRAP: SerialStatus.SCRAPPED,
    TransactionType.ADJUSTMENT_DECREASE: SerialStatus.SCRAPPED,
    # --- the unit stays ours, and its status follows the move ----------------
    TransactionType.TRANSFER_OUT: SerialStatus.IN_TRANSIT,
    TransactionType.TRANSFER_IN: SerialStatus.IN_STOCK,
    # --- the unit stays ours, and stays where it is -------------------------
    TransactionType.QC_HOLD: None,
    TransactionType.QC_RELEASE: None,
    TransactionType.SCRAP_DISPOSAL: None,
}


def movement_relocates_unit(source_state: StockState | None) -> bool:
    """Does this movement *put* the unit somewhere, or only act on it in place?

    One question, one answer, and it is the same one ``gates_policy`` asks in
    :func:`validate_movement`: a movement whose resolved source pool is ``None``
    brings stock into the position from outside it - a receipt, a return, the
    inbound leg of a transfer - and that is exactly the set of movements that
    decide where the unit now is. Everything else - an issue, a dispatch, a
    scrap, a QC hold, a QC release, the *outbound* leg of a transfer - happens
    *to* a unit that is already somewhere, and assigning the movement's own
    warehouse and bin to it is how a QC hold keyed at warehouse level silently
    emptied the bin a unit was sitting in, and how a hold raised against the
    wrong warehouse silently teleported it there.

    The argument is the **resolved** source, not the transaction type. Keyed on
    the type's default transition alone this was wrong for every movement that
    overrides the source: ``change_stock_state(JOB_WORK -> AVAILABLE)`` posts a
    ``JOB_WORK_RECEIPT``, whose *default* source is ``None``, but it takes the
    quantity out of this position's own job-work pool. It relocates nothing, and
    treating it as a relocation both skipped the "raise it where the unit is"
    check and let a return keyed at the wrong store rewrite the unit's position
    to that store.
    """
    return source_state is None


#: Movement types that relocate a unit **under their default transition**, which
#: is what a caller naming no ``stock_state`` and no ``from_state`` gets.
#:
#: Convenience for callers and tests that reason about plain postings. The
#: engine does not consult it: it asks :func:`movement_relocates_unit` about the
#: pools :func:`resolve_pools` actually produced, because a caller can override
#: the source and make a member of this set relocate nothing.
SERIAL_RELOCATING_TYPES: frozenset[TransactionType] = frozenset(
    transaction_type
    for transaction_type, transition in _DEFAULT_TRANSITION.items()
    if movement_relocates_unit(transition.source)
)


def assert_unit_is_where_the_movement_is(
    *,
    serial_number: str,
    transaction_type: TransactionType,
    relocates: bool,
    recorded_warehouse_id: uuid.UUID | None,
    movement_warehouse_id: uuid.UUID,
    recorded_location_id: uuid.UUID | None = None,
    movement_location_id: uuid.UUID | None = None,
) -> None:
    """A movement that does not relocate a unit must be raised where the unit is.

    ``warehouse_id`` and ``location_id`` on ``stock_serials`` record where the
    unit was last put, and only a relocating movement may rewrite them - see
    :func:`movement_relocates_unit`, whose answer ``relocates`` carries. That
    split is what stops a QC hold keyed against the wrong store from teleporting
    a unit - but on its own it left the other half open: the posting was still
    accepted, so the hold moved a *quantity* at a warehouse the unit is not in,
    and the unit's own status was then decided by a movement that happened
    somewhere else. A QC release raised at the wrong store put a returned unit
    back "into stock" at a store it had never been in.

    So the rule is symmetrical with the position one: a movement that does not
    move the unit has to name the position it is already at.

    ASSUMPTION: **both halves of the position are compared, but only where the
    movement states one.** A non-relocating movement is routinely raised at
    warehouse level while the unit's bin records the shelf it was last put on -
    assembly consumption, dispatch and QC decisions all post without a
    ``location_id`` - so a movement that names no bin is held to the warehouse
    alone, exactly as before. But a movement that *does* name one has stated
    where it thinks the unit is, and a bin that disagrees is the identical
    mis-key the warehouse rule exists for: the quantity moves at bin A while the
    unit sits on bin B, and the unit's status is decided by something that
    happened one aisle over. Only the unstated case is waived, never a stated
    one that is wrong.

    ASSUMPTION: a unit with no recorded warehouse, or no recorded bin, is
    accepted. NULL means the unit has been registered from a packing list and
    never positioned, or was positioned at warehouse level, so there is nothing
    for the movement to disagree with. Whether a unit may leave before it has
    ever arrived is a different rule, answered from the ledger by
    ``already_received``, not from a position column that was never written.
    """
    transaction_type = TransactionType(transaction_type)
    if relocates:
        return
    if recorded_warehouse_id is not None and recorded_warehouse_id != movement_warehouse_id:
        raise BusinessRuleError(
            f"Serial {serial_number} is held at another warehouse, and a {transaction_type} "
            "does not move it. Raise the movement where the unit is.",
            details={
                "serial_number": serial_number,
                "transaction_type": str(transaction_type),
                "serial_warehouse_id": str(recorded_warehouse_id),
                "movement_warehouse_id": str(movement_warehouse_id),
            },
        )
    if (
        movement_location_id is not None
        and recorded_location_id is not None
        and movement_location_id != recorded_location_id
    ):
        raise BusinessRuleError(
            f"Serial {serial_number} is held in another storage location, and a "
            f"{transaction_type} does not move it. Raise the movement where the unit is.",
            details={
                "serial_number": serial_number,
                "transaction_type": str(transaction_type),
                "serial_location_id": str(recorded_location_id),
                "movement_location_id": str(movement_location_id),
            },
        )


def serial_transition(
    *,
    serial_number: str,
    current: SerialStatus,
    transaction_type: TransactionType,
    landing_state: StockState | None,
    already_received: bool = False,
    source_state: StockState | None = None,
) -> SerialStatus | None:
    """The status this movement leaves the serial in, or ``None`` for no change.

    Raises rather than returning when the movement is not something that can be
    done to a unit in this state. Issuing a unit that is already issued is an
    error, not a no-op: the second issue is either a mis-scan or a unit that has
    physically left twice, and both need a person to look at them.

    ``landing_state`` is the pool the movement actually puts the quantity in -
    the target :func:`resolve_pools` works out from the type, the caller's
    ``stock_state`` and its ``from_state`` - and ``None`` when the quantity
    leaves the position altogether. It is required rather than inferred, because
    the type alone does not determine it: the same ``QC_RELEASE`` can land in
    the available pool or in the return pool depending on what the caller asked
    for, and the unit's status has to agree with where its quantity went.

    ``already_received`` closes the inbound half of the same rule. The
    ``IN_STOCK -> IN_STOCK`` self edge exists because a serial row is registered
    before the goods arrive and the vocabulary has no "expected" state, so the
    unit's first receipt starts from its own initial status. Left unqualified,
    that edge also accepts the *second* receipt of a unit that is already on the
    shelf - a re-scanned goods receipt books a second unit's worth of stock
    against one physical object. The caller answers "has a receipt already been
    posted for this unit?" from the ledger, which is the only place that knows
    the difference between a registration and an arrival.

    Pure, and separate from the write, so the graph can be exercised without a
    database. The caller does the read-check-write under the serial's row lock.
    """
    transaction_type = TransactionType(transaction_type)
    current = SerialStatus(current)
    target = SERIAL_MOVEMENT_TARGETS[transaction_type]

    # Processing draws a named input from the vendor pool before the processed
    # receipt returns that physical unit. It is already issued; issuing it a
    # second time from an ordinary warehouse remains an invalid transition.
    if (
        current is SerialStatus.ISSUED
        and source_state is StockState.JOB_WORK
        and transaction_type is TransactionType.MATERIAL_ISSUE
    ):
        return None
    if (
        current is SerialStatus.ISSUED
        and target is SerialStatus.SCRAPPED
        and not (transaction_type is TransactionType.SCRAP and source_state is StockState.JOB_WORK)
    ):
        raise BusinessRuleError("An issued serial can be scrapped only from its job-work pool.")

    # ASSUMPTION: passing inspection is what puts a customer return back into
    # normal stock. Every movement type that targets IN_STOCK also *adds* a unit
    # to on-hand, and a customer return has already added this one, so there was
    # no quantity-neutral way out of RETURNED at all - the graph declared the
    # edge and nothing could drive it, which made a returned unit unsellable
    # forever. A QC release is the movement that resolves an inspection and is
    # neutral on on-hand (it moves the quantity from the hold pool to the
    # available one), so it is the movement that resolves the unit's status too.
    # Writing a returned unit off, or sending it out again after refurbishment,
    # already had paths (SCRAP, DISPATCH) and keep them.
    #
    # ASSUMPTION: and only when the release actually lands the quantity in the
    # available pool. Keyed on the transaction type alone this fired for every
    # release, including one that puts the quantity back in the RETURN pool -
    # ``change_stock_state(QC_HOLD -> RETURN)``, which is also a ``QC_RELEASE``.
    # The unit was then IN_STOCK while its quantity was still classified as
    # customer-return stock, and the two halves of the same fact disagreed. The
    # pool is the statement about what the goods now are; the status follows it.
    if (
        target is None
        and transaction_type is TransactionType.QC_RELEASE
        and current is SerialStatus.RETURNED
        and landing_state is StockState.AVAILABLE
    ):
        target = SerialStatus.IN_STOCK

    if target is None:
        if current not in SERIAL_IN_WAREHOUSE_STATUSES:
            raise BusinessRuleError(
                f"Serial {serial_number} is {current} and is not in a warehouse, so it "
                f"cannot be handled by a {transaction_type} movement.",
                details={"serial_number": serial_number, "status": str(current)},
            )
        return None

    if already_received and target is SerialStatus.IN_STOCK and current is SerialStatus.IN_STOCK:
        raise BusinessRuleError(
            f"Serial {serial_number} is already in stock and has been received before; "
            f"a {transaction_type} would book a second unit against one physical object.",
            details={
                "serial_number": serial_number,
                "status": str(current),
                "transaction_type": str(transaction_type),
            },
        )

    # InvalidStateTransitionError from here, which is a BusinessRuleError
    # carrying the states the unit could legally have moved to instead.
    SERIAL_FLOW.assert_transition(current, target)
    return target


# -----------------------------------------------------------------------------
# Document header locking
# -----------------------------------------------------------------------------


async def _lock_document(
    session: AsyncSession,
    fetch: Callable[[uuid.UUID], Awaitable[Any]],
    entity: str,
    entity_id: uuid.UUID,
) -> Any:
    """Take a document header's row lock, bounded by the same timeout as stock.

    Every state change on a transfer, an adjustment or a count sheet is a
    read-modify-write over its status, so the header is the serialisation point
    for the whole document. The wait is bounded for exactly the reason
    :data:`LOCK_TIMEOUT_MS` exists: a contended posting must come back as a
    retryable 409 rather than hold an HTTP worker behind somebody else's
    transaction until the statement timeout fires.
    """
    await set_lock_timeout(session, LOCK_TIMEOUT_MS)
    try:
        row = await fetch(entity_id)
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            log.info("inventory.document_lock_timeout", entity=entity, entity_id=str(entity_id))
            inventory_conflicts.labels(operation="document").inc()
            raise StockLockTimeoutError(
                "This document is being updated by another operation. Please retry."
            ) from exc
        raise
    if row is None:
        raise NotFoundError(entity, entity_id)
    return row


# -----------------------------------------------------------------------------
# The engine
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MasterData:
    """Everything the engine validates a batch of movements against, loaded once."""

    items: dict[uuid.UUID, ItemFacts]
    warehouses: dict[uuid.UUID, WarehouseFacts]
    locations: dict[uuid.UUID, LocationFacts]
    #: The lot facts, not just its owner: the engine has to decide whether the
    #: lot is active and unexpired before it may receive or issue against it.
    batches: dict[uuid.UUID, BatchFacts]
    #: ``serial_id -> (item_id, serial_number)``. Both columns are immutable, so
    #: this is read *without* a lock and every pure master-data rule can be
    #: checked before the engine takes its first one - which is what lets a
    #: movement naming a bin or a unit that does not exist come back as a typed
    #: error rather than as an integrity error from the balance upsert.
    serial_owners: dict[uuid.UUID, tuple[uuid.UUID, str]]
    #: Whole ORM rows, not a projection, and locked ``FOR UPDATE``: the engine
    #: writes the serial's status back, so it has to hold the row. Empty until
    #: the balance rows are locked - the two lock sets are always acquired in
    #: that order, by every path, or two postings could take them the other way
    #: round and deadlock.
    serials: dict[uuid.UUID, StockSerial] = field(default_factory=dict)
    #: Units the ledger already holds a stock receipt for. The one fact that
    #: separates a serial registered from a packing list - which has never been
    #: received and whose first receipt is legal - from one that is on the shelf
    #: because a receipt was already posted for it. Mutable on purpose: a batch
    #: that names the same unit on two receipt lines has to refuse the second
    #: line as well, and the first line's ledger row has not been flushed yet.
    received_serials: set[uuid.UUID] = field(default_factory=set)
    job_work_serials: dict[uuid.UUID, tuple[str, uuid.UUID | None]] = field(default_factory=dict)


#: The serial columns a movement can change. Snapshotted before and after so the
#: audit row says what actually moved rather than "something was updated".
_SERIAL_AUDIT_FIELDS = ["status", "warehouse_id", "location_id"]


@dataclass(frozen=True, slots=True)
class _SerialChange:
    """A serial row this posting modified, and what it looked like beforehand.

    Collected during ``_apply`` - which is synchronous - and audited after the
    flush, in the same transaction as the ledger row that caused it. Both
    snapshots are taken *before* the flush on purpose: reading an attribute off
    an ORM row after a flush is how the ``MissingGreenlet`` trap in CLAUDE.md
    starts, and neither snapshot needs the database.
    """

    serial: StockSerial
    before: dict[str, Any]
    after: dict[str, Any]
    movement: MovementRequest


@dataclass(frozen=True, slots=True)
class _Applied:
    """One movement, after the engine has applied it to a locked position.

    Collected rather than returned piecemeal because three things downstream
    need different parts of it: the caller wants the ledger rows, the audit pass
    wants the movement beside its transaction, and an inbound leg carrying
    another leg's cost wants ``carried`` - the exact per-layer breakdown the
    outbound movement took, which does not exist until it has been applied.
    """

    movement: MovementRequest
    transaction: InventoryTransaction
    serial_change: _SerialChange | None
    carried: tuple[LayerCost, ...]
    uncovered_quantity: Decimal


#: A position holding stock its cost layers do not account for. Repaired by
#: opening a layer for the gap.
LAYER_RECONCILIATION_SHORTFALL = "SHORTFALL"

#: A position whose cost layers account for stock it does not hold. Repaired by
#: consuming the excess out of them, oldest first.
LAYER_RECONCILIATION_EXCESS = "EXCESS"


@dataclass(frozen=True, slots=True)
class _LayerReconciliation:
    """A position whose cost layers disagreed with its on-hand, and the repair.

    Audited like any other change to a balance: the position's valuation basis
    moved, and a figure on the balance sheet that changed without a document
    behind it is exactly the thing an audit trail exists for.

    ``direction`` says which way it disagreed and therefore which repair ran -
    :data:`LAYER_RECONCILIATION_SHORTFALL` or :data:`LAYER_RECONCILIATION_EXCESS`.
    ``value`` is signed: what the repair added to the position's value, or took
    out of it. ``pool`` says which of the position's two queues was repaired; a
    scrap-queue repair changes no book value, only the cost a later disposal
    will be measured at.
    """

    direction: str
    grain: BalanceGrain
    balance_id: uuid.UUID
    item_code: str
    quantity: Decimal
    unit_cost: Decimal
    value: Decimal
    pool: CostLayerPool = CostLayerPool.ON_HAND


class MovementEngine:
    """Posts stock movements. Everything that changes stock instantiates this."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.balances = InventoryRepository(session, tenant)
        self.ledger = LedgerRepository(session, tenant)
        self.serials = StockSerialRepository(session, tenant)
        self.batches = StockBatchRepository(session, tenant)
        self.cost_layers = CostLayerRepository(session, tenant)
        self.consumptions = CostConsumptionRepository(session, tenant)

    # --- locking ------------------------------------------------------------

    async def lock(self, grains: list[BalanceGrain]) -> dict[BalanceGrain, InventoryBalance]:
        """Take the row locks for a set of positions and re-read them.

        Exposed separately for the operations that must *read* the current
        quantity before they know what to post - a physical count works out its
        variance from the locked value, not from the snapshot it took days ago.
        """
        assert_warehouse_scope(self.tenant, *(grain.warehouse_id for grain in grains))
        await set_lock_timeout(self.session, LOCK_TIMEOUT_MS)
        try:
            return await self.balances.lock_balances(grains)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
                log.info("inventory.lock_timeout", grain_count=len(grains))
                inventory_conflicts.labels(operation="balance").inc()
                raise StockLockTimeoutError() from exc
            raise

    async def lock_layers(
        self, balances: Collection[InventoryBalance]
    ) -> dict[uuid.UUID, list[InventoryCostLayer]]:
        """Take the row locks on every open cost layer at these positions.

        **Always after** :meth:`lock` and **always before** :meth:`_lock_serials`.
        Consuming a layer is a read-modify-write on ``remaining_quantity``, and
        two issues that both read the same remaining quantity would both decide
        they may spend it. The balance row lock already serialises two postings
        at one position, so this is a second guard rather than the only one -
        but it is also what forces the re-read: without
        ``populate_existing`` SQLAlchemy hands back the identity map's copy and
        the whole acquisition is a no-op.

        Layers are locked for **every** position the batch locked, including a
        transfer's destination, whose in-transit quantity changes without any
        stock moving through it. A subset would be a second, shorter lock order,
        and two documents holding different subsets of the same positions is how
        a deadlock is built.
        """
        balance_ids = [balance.id for balance in balances]
        if not balance_ids:
            return {}
        await set_lock_timeout(self.session, LOCK_TIMEOUT_MS)
        try:
            return await self.cost_layers.lock_open_layers(balance_ids)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
                log.info("inventory.layer_lock_timeout", position_count=len(balance_ids))
                inventory_conflicts.labels(operation="cost_layer").inc()
                raise StockLockTimeoutError() from exc
            raise

    # --- posting ------------------------------------------------------------

    async def post(self, movements: list[MovementRequest]) -> list[InventoryTransaction]:
        transactions, _ = await self.post_with_balances(movements)
        return transactions

    async def post_with_balances(
        self,
        movements: list[MovementRequest],
        *,
        extra_grains: list[BalanceGrain] | None = None,
        prelocked: dict[BalanceGrain, InventoryBalance] | None = None,
        auto_consume_batches: bool = True,
    ) -> tuple[list[InventoryTransaction], dict[BalanceGrain, InventoryBalance]]:
        """Post a batch of movements atomically.

        ``extra_grains`` are positions that are not moved by any line but are
        still written in the same operation - the destination of a transfer,
        whose in-transit quantity changes. They are locked together with the
        movement grains so a transfer A->B and a transfer B->A cannot take the
        two warehouses' locks in opposite orders and deadlock.

        ``prelocked`` lets a caller that has already locked (via :meth:`lock`)
        skip the balance acquisition. The FIFO cost layers are locked here in
        either case, always after the balances and always for every position the
        batch holds, so the acquisition order is identical on every path.
        """
        if not movements:
            return [], prelocked or {}

        extra = list(dict.fromkeys(extra_grains or ()))
        # Every touched grain is authorized before any lock/upsert or stock work.
        # Trusted system contexts have empty assignments and retain company scope.
        assert_warehouse_scope(
            self.tenant,
            *(movement.warehouse_id for movement in movements),
            *(grain.warehouse_id for grain in extra),
            *(grain.warehouse_id for grain in (prelocked or {})),
        )
        async with transaction(self.session):
            # FEFO is a decision made before validation, because the chosen lot
            # is itself a master-data fact the movement is then held to. An
            # explicit lot is never overridden.
            movements = await self._assign_fefo_batches(movements)
            master = await self._load_master_data(movements, extra)
            # Master data is validated before the first lock is taken. The
            # balance upsert has foreign keys of its own, so a movement naming a
            # storage location that does not exist would otherwise be answered
            # by an integrity error from PostgreSQL instead of by a typed error
            # naming the bin (section 53).
            for movement in movements:
                self._validate(movement, master)
            # Before any lock, because it is a programming error rather than a
            # data one and costs nothing to check.
            self._assert_cost_carriers(movements)
            # And an ``extra_grain`` is locked, created and written exactly like
            # a movement's own position, so it is held to the same rules. It was
            # not, and a stock transfer whose destination bin belonged to another
            # tenant - or to a third warehouse - dispatched happily, wrote the
            # in-transit quantity into a position no warehouse view will ever
            # show, and then could never be received: the inbound leg *is*
            # validated, so it refused the same bin the outbound leg had
            # accepted, and the document sat in IN_TRANSIT with no edge out of
            # it. ``receiving=True`` because a destination position takes stock
            # in, which is the direction the active-flag rules care about.
            for grain in extra:
                self._validate_grain(grain, master, receiving=True)

            grains = [m.grain for m in movements] + extra
            locked = prelocked if prelocked is not None else await self.lock(grains)
            missing = [g for g in grains if g not in locked]
            if missing:  # pragma: no cover - lock_balances creates every grain
                raise BusinessRuleError(
                    "A stock position could not be locked for update.",
                    details={"positions": len(missing)},
                )

            # The FIFO cost layers, locked immediately after the balance rows
            # and before the serials. The order of the three lock sets -
            # balances, layers, serials, each by primary key - is the same on
            # every path through this engine, including the callers that
            # pre-lock the balances themselves, so two postings touching an
            # overlapping set queue instead of deadlocking.
            #
            # A position keeps two queues (``CostLayerPool``): the on-hand queue
            # every valuation rests on, and the scrap queue only a scrap
            # disposal draws. They are locked in one acquisition and split
            # here, before anything is costed against either.
            locked_layers = await self.lock_layers(locked.values())
            layers = {
                balance_id: [layer for layer in rows if layer.pool is CostLayerPool.ON_HAND]
                for balance_id, rows in locked_layers.items()
            }
            scrap_layers = {
                balance_id: [layer for layer in rows if layer.pool is CostLayerPool.SCRAP]
                for balance_id, rows in locked_layers.items()
            }
            reconciled = self._reconcile_layers(
                [movement.grain for movement in movements], locked, layers, master
            )
            # The scrap queue is repaired only where a movement is about to draw
            # on it. Scrap landing on a position whose queue is short is simply
            # appended behind the gap, and the gap is closed - at the front of
            # the queue, where untracked scrap belongs - by the first disposal.
            reconciled += self._reconcile_layers(
                [movement.grain for movement in movements if draws_on_scrap(movement)],
                locked,
                scrap_layers,
                master,
                pool=CostLayerPool.SCRAP,
            )

            serial_ids_locked = await self._lock_serials(movements)
            master = replace(
                master,
                serials=serial_ids_locked,
                received_serials=await self._received_serials(movements, serial_ids_locked),
                job_work_serials=await self._job_work_serials(movements),
            )

            transactions: list[InventoryTransaction] = []
            serial_changes: list[_SerialChange] = []
            applied: list[_Applied] = []
            for movement in movements:
                # ``carries_cost_of`` is resolved here rather than inside
                # ``_apply``: the donor movement must already have been applied
                # for its draw to exist, which is exactly what "an earlier index
                # in this batch" means, and the ordering is this loop's.
                carried = (
                    applied[movement.carries_cost_of].carried
                    if movement.carries_cost_of is not None
                    else movement.cost_layers
                )
                step = self._apply(
                    movement, locked, layers, master, carried=carried, scrap_layers=scrap_layers
                )
                applied.append(step)
                transactions.append(step.transaction)
                if step.serial_change is not None:
                    serial_changes.append(step.serial_change)

            # One flush for the whole batch: the ledger rows, the cost layers
            # and the consumption rows are inserted together and the balance
            # updates go out as one UPDATE per row. The audit rows written
            # below are flushed once as well.
            await self.session.flush()

            if auto_consume_batches:
                await self.consume_empty_batches(movements)

            for step in applied:
                await self._audit(step, master)
            for serial_change in serial_changes:
                await self._audit_serial(serial_change)
            for entry in reconciled:
                await self._audit_reconciliation(entry)
            # One flush for the whole document's audit rows too - each _audit*
            # passes flush=False - so a 500-line document costs one round-trip,
            # not 500, while the balance, layer and serial row locks are held.
            await self.session.flush()

        return transactions, locked

    async def _assign_fefo_batches(self, movements: list[MovementRequest]) -> list[MovementRequest]:
        """Fill in a lot for issue movements that did not name one.

        The caller keeps the right to choose; this only answers the question
        when it was left open. Candidates are active, unexpired and hold enough
        *available* quantity at the exact position the movement addresses. If
        no single lot can cover the whole line, the movement is left alone and
        the ordinary stock-shortfall error explains the position.
        """
        wanted = [
            index
            for index, movement in enumerate(movements)
            if movement.batch_id is None
            and movement.serial_id is None
            and movement.transaction_type in FEFO_MOVEMENTS
        ]
        if not wanted:
            return movements
        items = await self.balances.item_facts([movements[index].item_id for index in wanted])
        assigned = list(movements)
        for index in wanted:
            movement = movements[index]
            item = items.get(movement.item_id)
            if item is None or not item.is_batch_tracked:
                continue
            lots = await self.balances.fefo_candidates(
                item_id=movement.item_id,
                warehouse_id=movement.warehouse_id,
                location_id=movement.location_id,
                minimum_quantity=movement.quantity,
                as_of=today(),
            )
            if lots:
                assigned[index] = replace(movement, batch_id=lots[0].batch_id)
        return assigned

    async def consume_empty_batches(self, movements: Sequence[MovementRequest]) -> None:
        """Mark a lot CONSUMED when the company no longer owns any of it.

        The total includes in-transit, job-work and scrap quantities, not only
        on-hand: a lot with units on a lorry or at a plater is not finished.
        The transfer dispatch path suppresses this and calls it again after it
        has moved the quantity into the destination's in-transit pool.
        """
        batch_ids = [m.batch_id for m in movements if m.batch_id is not None]
        if not batch_ids:
            return
        totals = await self.balances.batch_totals(batch_ids, scoped=False)
        for batch_id, quantity in totals.items():
            if quantity.owned != 0:
                continue
            batch = await self.batches.lock_for_update(batch_id)
            if batch is None or BatchStatus(batch.status) is BatchStatus.CONSUMED:
                continue
            previous = BatchStatus(batch.status)
            BATCH_FLOW.assert_transition(previous, BatchStatus.CONSUMED)
            batch.status = BatchStatus.CONSUMED
            self.batches.touch(batch)
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_BATCH,
                entity_id=batch.id,
                entity_code=batch.batch_number,
                from_status=str(previous),
                to_status=str(BatchStatus.CONSUMED),
                user_id=self.tenant.user_id,
                reason="Batch quantity reached zero.",
            )
        await self.session.flush()

    @staticmethod
    def _assert_cost_carriers(movements: list[MovementRequest]) -> None:
        """``carries_cost_of`` must point backwards, from an inflow to an outflow.

        A programming error rather than a user one, so it is refused loudly
        instead of being quietly ignored: a transfer whose inbound leg silently
        failed to pick up its outbound leg's layers would re-cost the arrival
        from standard cost and change the company's stock value by moving a box,
        with nothing anywhere saying so.
        """
        for index, movement in enumerate(movements):
            source = movement.carries_cost_of
            if source is None:
                continue
            if not 0 <= source < index:
                raise ValidationError(
                    "A movement can only carry the cost of an earlier movement in the batch.",
                    details={"index": index, "carries_cost_of": source},
                )
            if (
                movement_effect(
                    movement.transaction_type, movement.stock_state, movement.from_state
                ).on_hand_quantity
                <= 0
            ):
                raise ValidationError(
                    "Only a movement that brings stock in can carry another movement's cost.",
                    details={"transaction_type": str(movement.transaction_type)},
                )
            donor = movements[source]
            if (
                movement_effect(
                    donor.transaction_type, donor.stock_state, donor.from_state
                ).on_hand_quantity
                >= 0
            ):
                raise ValidationError(
                    "A movement can only carry the cost of one that takes stock out.",
                    details={"transaction_type": str(donor.transaction_type)},
                )

    def _reconcile_layers(
        self,
        grains: Sequence[BalanceGrain],
        locked: dict[BalanceGrain, InventoryBalance],
        layers: dict[uuid.UUID, list[InventoryCostLayer]],
        master: _MasterData,
        *,
        pool: CostLayerPool = CostLayerPool.ON_HAND,
    ) -> list[_LayerReconciliation]:
        """Make a position's cost layers add up to its on-hand quantity again.

        ``pool`` picks the queue: the on-hand queue against ``on_hand_quantity``
        (everything below), or the scrap queue against ``scrap_quantity``. The
        scrap queue is repaired by exactly the same rules for exactly the same
        reasons - scrap that predates the queue, or that a missed movement left
        uncovered - with one difference that makes it cheaper: it carries no
        book value, so a repair there changes the cost a later disposal is
        measured at and nothing on the balance sheet.

        ASSUMPTION: a position whose cost layers disagree with its
        ``on_hand_quantity`` is repaired in **either** direction, visibly, at the
        cost the position itself already claims - rather than refused, left
        under-valued, or left over-valued. The reasoning is below; the
        alternatives and why they are worse are the point of it.

        The invariant FIFO valuation rests on is that a position's open layers
        add up to its ``on_hand_quantity``, and their value to its
        ``stock_value``. The engine maintains it on every posting, and on-hand
        can never go negative, so nothing the engine does can break it. Two
        things outside the engine can:

        * stock that predates FIFO valuation and that the migration's backfill
          did not reach - a position at a grain the backfill's filter missed;
        * a movement posted by the previous deployment in the window between the
          migration running and the new code rolling out, which moved on-hand
          without touching a layer.

        Either of those can leave the layers **short** of the stock or **over**
        it, depending on which way the missed movement went, and the repair has
        to be symmetric because the failure is.

        *Short* - the position holds stock the layers say is worth nothing. The
        gap is closed with one layer, at the position's own recorded
        ``average_cost``. That invents nothing: the balance already claimed
        ``on_hand x average_cost``, and this is the same number written where
        FIFO can see it. It is dated *before* every layer already at the
        position, because it is the oldest stock there - untracked goods arrived
        before anything the system can account for - and consuming it later
        would be the wrong end of the queue.

        *Over* - the layers account for stock that is not there. The excess is
        consumed out of them **oldest first**, which is a replay of the movement
        the layers missed: that movement took stock out, and FIFO says it took
        the oldest. Nothing physical is destroyed - ``on_hand_quantity`` is not
        touched - what goes is value the position was carrying for goods it does
        not have.

        A previous round left this direction unrepaired, on the grounds that
        trimming layers "destroys value silently, which is worse than a visible
        discrepancy". The discrepancy was not visible: a WARNING in a log is not
        the balance sheet, and the very next ordinary posting recomputed
        ``stock_value`` from the layers and *inflated the position back up*,
        because the engine believes the layers over the balance. A silent
        inflation overstates the balance sheet, which is worse than the
        deflation it was avoiding, not better. So both directions are repaired
        and neither is silent: each writes a WARNING **and** an audit row against
        the balance naming the direction, the quantity and the value moved.
        """
        repairs: list[_LayerReconciliation] = []
        for grain in dict.fromkeys(grains):
            balance = locked[grain]
            open_layers = layers.setdefault(balance.id, [])
            held = Decimal(
                balance.on_hand_quantity
                if pool is CostLayerPool.ON_HAND
                else balance.scrap_quantity
            )
            covered = sum(
                (Decimal(layer.remaining_quantity) for layer in open_layers), Decimal("0")
            )
            if covered == held:
                continue

            item = master.items[grain.item_id]
            if covered > held:
                repairs.append(
                    self._trim_excess_layers(
                        grain,
                        balance,
                        open_layers,
                        item,
                        to_quantity(covered - held),
                        pool=pool,
                    )
                )
                continue

            shortfall = to_quantity(held - covered)
            # ASSUMPTION: an uncovered scrap quantity is priced like an
            # uncovered on-hand one - the position's recorded cost, else the
            # item's standard cost. The migration that opened the scrap queue
            # priced existing scrap from the movements that scrapped it; this is
            # only the floor beneath it. docs/assumptions.md §E8-2.
            unit_cost = _first_positive(Decimal(balance.average_cost), item.standard_cost)
            received_at = self._reconciliation_timestamp(balance, open_layers)
            layer = InventoryCostLayer(
                id=new_id(),
                balance_id=balance.id,
                item_id=grain.item_id,
                warehouse_id=grain.warehouse_id,
                location_id=grain.location_id,
                batch_id=grain.batch_id,
                received_at=received_at,
                original_quantity=shortfall,
                remaining_quantity=shortfall,
                unit_cost=unit_cost,
                pool=pool,
                origin=LAYER_ORIGIN_RECONCILIATION,
            )
            self.cost_layers.add(layer)
            open_layers.append(layer)
            open_layers.sort(key=fifo_sort_key)
            log.warning(
                "inventory.fifo_layer_reconciliation",
                item_code=item.code,
                warehouse_id=str(grain.warehouse_id),
                pool=str(pool),
                quantity=str(shortfall),
                unit_cost=str(unit_cost),
            )
            repairs.append(
                _LayerReconciliation(
                    direction=LAYER_RECONCILIATION_SHORTFALL,
                    grain=grain,
                    item_code=item.code,
                    quantity=shortfall,
                    unit_cost=unit_cost,
                    value=to_stock_value(shortfall * unit_cost),
                    balance_id=balance.id,
                    pool=pool,
                )
            )
        return repairs

    def _trim_excess_layers(
        self,
        grain: BalanceGrain,
        balance: InventoryBalance,
        open_layers: list[InventoryCostLayer],
        item: ItemFacts,
        excess: Decimal,
        *,
        pool: CostLayerPool = CostLayerPool.ON_HAND,
    ) -> _LayerReconciliation:
        """Consume the quantity the layers claim over the position's on-hand.

        Oldest first, through the same :func:`consume_layers` an issue uses, so
        the repair is a replay of the movement the layers missed rather than a
        second, differently-ordered rule. An emptied layer is left in place with
        ``remaining_quantity`` at zero and dropped from the live queue - exactly
        what a real consumption does - because the consumption rows of past
        issues point at it.

        No ``inventory_cost_consumptions`` row is written: those carry a
        ``transaction_id`` and there is no transaction here. The audit row the
        caller writes is the trace, which is why this returns one rather than
        only doing the work.
        """
        consumption = consume_layers(
            [
                LayerView(
                    remaining=Decimal(layer.remaining_quantity),
                    unit_cost=Decimal(layer.unit_cost),
                )
                for layer in open_layers
            ],
            excess,
            # Unreachable: the layers hold more than ``excess`` by construction.
            uncovered_unit_cost=Decimal("0"),
        )
        for draw in consumption.draws:
            layer = open_layers[draw.index]
            layer.remaining_quantity = to_quantity(
                Decimal(layer.remaining_quantity) - draw.quantity
            )
            self.cost_layers.touch(layer)
        open_layers[:] = [layer for layer in open_layers if Decimal(layer.remaining_quantity) > 0]

        removed = consumption.layer_value
        unit_cost = to_unit_price(removed / excess) if excess else Decimal("0.000000")
        log.warning(
            "inventory.fifo_layers_exceed_on_hand",
            item_code=item.code,
            warehouse_id=str(grain.warehouse_id),
            pool=str(pool),
            on_hand=str(Decimal(balance.on_hand_quantity)),
            scrap=str(Decimal(balance.scrap_quantity)),
            excess_quantity=str(excess),
            value_removed=str(to_stock_value(removed)),
        )
        return _LayerReconciliation(
            direction=LAYER_RECONCILIATION_EXCESS,
            grain=grain,
            item_code=item.code,
            quantity=excess,
            unit_cost=unit_cost,
            value=-to_stock_value(removed),
            balance_id=balance.id,
            pool=pool,
        )

    @staticmethod
    def _reconciliation_timestamp(
        balance: InventoryBalance, open_layers: Sequence[InventoryCostLayer]
    ) -> datetime:
        """A timestamp that sorts before every layer already at the position.

        A microsecond earlier than the oldest of them, not equal to it: the FIFO
        key breaks a tie on the UUIDv7 primary key, and this layer's id is the
        newest one in the system, so an equal timestamp would sort it *last* -
        the opposite of what untracked opening stock is.
        """
        stamp = balance.created_at or utcnow()
        if stamp.tzinfo is None:  # pragma: no cover - TIMESTAMPTZ comes back aware
            stamp = stamp.replace(tzinfo=UTC)
        if open_layers:
            earliest = fifo_sort_key(open_layers[0])[0] - timedelta(microseconds=1)
            stamp = min(stamp, earliest)
        return stamp

    # --- internals ----------------------------------------------------------

    async def _load_master_data(
        self,
        movements: list[MovementRequest],
        grains: Sequence[BalanceGrain] = (),
    ) -> _MasterData:
        """Five queries for the whole batch, never one per line (section 32).

        Every one of them is a fixed cost: the id lists are de-duplicated and
        each lookup is a single ``IN`` query, so a two-hundred-line document
        costs exactly the same number of round trips as a one-line one. The
        positions in ``grains`` - destinations a document writes without moving
        anything through them - are folded into the same id lists rather than
        fetched separately, so validating them costs no extra round trip at all.
        """
        items = await self.balances.item_facts(
            [m.item_id for m in movements] + [g.item_id for g in grains]
        )
        warehouses = await self.balances.warehouse_facts(
            [m.warehouse_id for m in movements] + [g.warehouse_id for g in grains]
        )
        locations = await self.balances.location_facts(
            [m.location_id for m in movements if m.location_id is not None]
            + [g.location_id for g in grains if g.location_id is not None]
        )
        batches = await self.balances.batch_facts(
            [m.batch_id for m in movements if m.batch_id is not None]
        )
        serial_owners = await self.balances.serial_owners(
            [m.serial_id for m in movements if m.serial_id is not None]
        )
        return _MasterData(
            items=items,
            warehouses=warehouses,
            locations=locations,
            batches=batches,
            serial_owners=serial_owners,
        )

    async def validate_positions(
        self,
        grains: Sequence[BalanceGrain],
        *,
        receiving: Collection[BalanceGrain] = (),
    ) -> None:
        """Refuse a set of positions that must not be created, in three queries.

        For the callers that lock a position *before* they know what to post -
        :func:`transfer_stock` is this module's - because ``lock_balances``
        creates the row it locks. A grain naming a bin that belongs to another
        tenant, or to a different warehouse, would otherwise be answered by a
        foreign-key violation carrying SQL or, worse, be created: a balance row
        at a grain no warehouse view will ever show, holding stock that is
        simply lost.

        Three queries for the whole set, whatever its size. Grains listed in
        ``receiving`` will take stock in, so the inactive-warehouse and
        inactive-bin rules apply to them; the rest are held only to existence,
        tenancy and warehouse membership, which are the rules with no direction.
        """
        if not grains:
            return
        wanted = list(dict.fromkeys(grains))
        assert_warehouse_scope(self.tenant, *(grain.warehouse_id for grain in wanted))
        master = await self._load_master_data([], wanted)
        inbound = set(receiving)
        for grain in wanted:
            self._validate_grain(grain, master, receiving=grain in inbound)

    def _validate_grain(self, grain: BalanceGrain, master: _MasterData, *, receiving: bool) -> None:
        """The master-data rules a *position* must satisfy, with no movement.

        The same phantom-position rules :func:`validate_movement` applies to a
        movement's own bin - the row exists, it is this tenant's, and it is in
        the warehouse the position names - because the balance row this grain
        keys is created, locked and written exactly like a movement's own.

        A ``receiving`` grain is held to **all three** active-flag rules an
        inbound movement is held to, and the set has to stay identical: this is
        the check a stock transfer's dispatch runs against its destination, and
        the rules ``receive`` will apply to the same destination as a real
        movement are the ones it has to agree with. It checked the warehouse and
        the bin but not the item, and that one missing line was enough: an item
        deactivated between create and dispatch let the outbound leg post - a
        deactivated item may still give stock up - and then ``receive`` refused
        the inbound leg, with no edge out of IN_TRANSIT except RECEIVED.

        Traceability is deliberately not here. A grain says nothing about
        serials, and demanding a batch of a *position* would refuse the very
        thing the direction gate exists to keep movable: legacy stock sitting at
        a batch-NULL grain because the flag was flipped over it. The transfer
        document answers for its own lines instead, at creation - see
        :meth:`StockTransferService._assert_lines_are_traceable`.
        """
        # Existence first, for both, then the flags - so a grain that is wrong
        # in two ways is answered the way :meth:`_validate` answers a movement
        # that is wrong in the same two ways.
        item = master.items.get(grain.item_id)
        if item is None:
            raise NotFoundError("Item", grain.item_id)
        warehouse = master.warehouses.get(grain.warehouse_id)
        if warehouse is None:
            raise NotFoundError("Warehouse", grain.warehouse_id)

        if receiving and not item.is_active:
            raise BusinessRuleError(
                inactive_record("Item", item.code, "cannot receive stock"),
                details={"item_code": item.code},
            )
        if receiving and not warehouse.is_active:
            raise BusinessRuleError(
                inactive_record("Warehouse", warehouse.code, "cannot receive stock"),
                details={"warehouse_code": warehouse.code},
            )

        if grain.location_id is None:
            return
        # Absent means "another company's, soft-deleted, or never existed at
        # all", and the three are one answer here - the same one
        # :meth:`_validate` gives a movement naming such a bin.
        location = master.locations.get(grain.location_id)
        if location is None:
            raise NotFoundError("Storage location", grain.location_id)
        if location.warehouse_id != grain.warehouse_id:
            raise BusinessRuleError(
                f"Storage location {location.code} is not in warehouse {warehouse.code}.",
                details={
                    "location_id": str(grain.location_id),
                    "location_code": location.code,
                    "warehouse_code": warehouse.code,
                    "location_warehouse_id": str(location.warehouse_id),
                },
            )
        if receiving and not location.is_active:
            raise BusinessRuleError(
                inactive_record("Storage location", location.code, "cannot receive stock"),
                details={
                    "location_id": str(grain.location_id),
                    "location_code": location.code,
                    "warehouse_code": warehouse.code,
                },
            )

    async def _received_serials(
        self, movements: list[MovementRequest], serials: dict[uuid.UUID, StockSerial]
    ) -> set[uuid.UUID]:
        """Which of the units this batch would receive have been received before.

        One indexed query for the whole batch, and only for the units the
        question can be asked of: a movement that takes a unit *out* of stock,
        or one whose unit is not currently ``IN_STOCK``, is decided by the graph
        alone and needs no ledger read. A batch that receives nothing - every
        issue, every dispatch, every movement of a fungible item - issues no
        query at all.
        """
        candidates: list[uuid.UUID] = []
        for movement in movements:
            if movement.serial_id is None:
                continue
            target = SERIAL_MOVEMENT_TARGETS[TransactionType(movement.transaction_type)]
            if target is not SerialStatus.IN_STOCK:
                continue
            serial = serials.get(movement.serial_id)
            if serial is not None and SerialStatus(serial.status) is SerialStatus.IN_STOCK:
                candidates.append(movement.serial_id)
        if not candidates:
            return set()
        return await self.ledger.received_serial_ids(candidates)

    async def _job_work_serials(
        self, movements: list[MovementRequest]
    ) -> dict[uuid.UUID, tuple[str, uuid.UUID | None]]:
        candidates = {
            movement.serial_id
            for movement in movements
            if movement.serial_id is not None
            and (
                resolve_pools(
                    movement.transaction_type,
                    stock_state=movement.stock_state,
                    from_state=movement.from_state,
                )[0]
                is StockState.JOB_WORK
                or movement.transaction_type is TransactionType.JOB_WORK_ISSUE
            )
        }
        if not candidates:
            return {}
        rows = await self.session.scalars(
            select(InventoryTransaction)
            .where(
                InventoryTransaction.company_id == self.tenant.company_id,
                InventoryTransaction.serial_id.in_(candidates),
            )
            .distinct(InventoryTransaction.serial_id)
            .order_by(
                InventoryTransaction.serial_id,
                InventoryTransaction.created_at.desc(),
                InventoryTransaction.id.desc(),
            )
        )
        return {
            row.serial_id: (row.reference_type, row.reference_id)
            for row in rows
            if row.serial_id is not None and row.transaction_type is TransactionType.JOB_WORK_ISSUE
        }

    async def _lock_serials(self, movements: list[MovementRequest]) -> dict[uuid.UUID, StockSerial]:
        """Take the row lock on every serial the batch names, and re-read them.

        The same primitive as the balance lock, for the same reason: a serial's
        status is read, decided on and written back, and a value read before the
        lock is worthless. Without it two concurrent issues of one unit both
        read ``IN_STOCK``, both decide the issue is legal, and the unit leaves
        the building twice.
        """
        serial_ids = [m.serial_id for m in movements if m.serial_id is not None]
        if not serial_ids:
            return {}
        await set_lock_timeout(self.session, LOCK_TIMEOUT_MS)
        try:
            return await self.serials.lock_serials(serial_ids)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
                log.info("inventory.serial_lock_timeout", serial_count=len(serial_ids))
                inventory_conflicts.labels(operation="serial").inc()
                raise StockLockTimeoutError(
                    "That serial number is being posted by another operation. Please retry."
                ) from exc
            raise

    def _validate(self, movement: MovementRequest, master: _MasterData) -> None:
        item = master.items.get(movement.item_id)
        if item is None:
            raise NotFoundError("Item", movement.item_id)
        warehouse = master.warehouses.get(movement.warehouse_id)
        if warehouse is None:
            raise NotFoundError("Warehouse", movement.warehouse_id)

        location: LocationFacts | None = None
        if movement.location_id is not None:
            location = master.locations.get(movement.location_id)
            # A bin missing from the map belongs to another company, has been
            # deleted, or never existed at all. The three are one answer here,
            # and none of them may reach the balance upsert: the position such a
            # movement keys is one no warehouse view will ever show, so the
            # stock booked into it is simply lost. Which warehouse the bin is
            # in, and whether it is still in use, are decided by
            # :func:`validate_movement` below with the rest of the master-data
            # rules.
            if location is None:
                raise NotFoundError("Storage location", movement.location_id)

        if movement.batch_id is not None and movement.batch_id not in master.batches:
            raise NotFoundError("Stock batch", movement.batch_id)
        if movement.serial_id is not None and movement.serial_id not in master.serial_owners:
            raise NotFoundError("Stock serial", movement.serial_id)

        batch = master.batches.get(movement.batch_id) if movement.batch_id else None
        validate_movement(
            movement,
            item=item,
            warehouse=warehouse,
            location=location,
            batch_owner=(batch.item_id, batch.batch_number) if batch else None,
            batch_status=batch.status if batch else None,
            batch_expires_on=batch.expires_on if batch else None,
            serial_owner=(
                master.serial_owners.get(movement.serial_id) if movement.serial_id else None
            ),
        )

    def _apply(
        self,
        movement: MovementRequest,
        locked: dict[BalanceGrain, InventoryBalance],
        layers: dict[uuid.UUID, list[InventoryCostLayer]],
        master: _MasterData,
        *,
        carried: Sequence[LayerCost] = (),
        scrap_layers: dict[uuid.UUID, list[InventoryCostLayer]] | None = None,
    ) -> _Applied:
        # Validated before any lock was taken, so both lookups are present.
        item = master.items[movement.item_id]
        warehouse = master.warehouses[movement.warehouse_id]

        signed = signed_quantity(movement.transaction_type, movement.quantity)
        magnitude = abs(signed)
        # Resolved once and handed to both halves: the balance columns are moved
        # by the deltas these pools imply, and the unit's status is decided by
        # the pool the quantity lands in. Working them out twice is how the two
        # came to disagree.
        source_state, landing_state = resolve_pools(
            movement.transaction_type, movement.stock_state, movement.from_state
        )
        effect = balance_effect(source_state, landing_state)
        balance = locked[movement.grain]

        columns = ("on_hand_quantity", *_POOL_COLUMNS)
        before = {name: Decimal(getattr(balance, name)) for name in columns}
        deltas = effect.deltas(magnitude)
        after = {name: before[name] + deltas.get(name, Decimal("0")) for name in columns}

        # The unit's own lifecycle is decided here - under its row lock, and
        # before the position is checked. Deciding is separate from writing so
        # an illegal scan raises having touched nothing, and it comes first
        # because it is the better answer: a storekeeper who re-scans a unit
        # that has already left needs to be told *that*, not that the position
        # happens to be one short. With any other unit of the same item on the
        # shelf the quantity check would not even notice.
        serial, serial_target = self._decide_serial(movement, master, source_state, landing_state)

        shortfall = find_shortfall(
            before=before,
            after=after,
            reserved=Decimal(balance.reserved_quantity),
            allow_negative_available=warehouse.allow_negative_stock,
        )
        if shortfall is not None:
            self._raise_shortfall(shortfall, item, warehouse, magnitude)

        serial_change = (
            None
            if serial is None
            else self._advance_serial(
                serial,
                serial_target,
                movement,
                relocates=movement_relocates_unit(source_state),
            )
        )

        # The cost layers of this position, already locked and re-read, in FIFO
        # order. ``_reconcile_layers`` has run, so in a healthy position their
        # remaining quantities add up to ``on_hand_quantity`` exactly - and the
        # scrap queue to ``scrap_quantity``, wherever this batch draws on it.
        open_layers = layers.setdefault(balance.id, [])
        scrap_queue = (scrap_layers if scrap_layers is not None else {}).setdefault(balance.id, [])
        # A movement out of scrap is costed against the scrap queue, and it is
        # the only kind that is: ``validate_movement`` refuses one that lands
        # anywhere but outside the position. Scrap carries no book value, so the
        # position's own value and average are left exactly as they were.
        from_scrap = source_state is StockState.SCRAP
        if from_scrap:
            valuation = value_movement(
                on_hand_after=after["scrap_quantity"],
                magnitude=magnitude,
                on_hand_delta=-magnitude,
                layers=[
                    LayerView(
                        remaining=Decimal(layer.remaining_quantity),
                        unit_cost=Decimal(layer.unit_cost),
                    )
                    for layer in scrap_queue
                ],
                average_before=Decimal(balance.average_cost),
                movement_unit_cost=movement.unit_cost,
                fallback_cost=item.standard_cost,
            )
        else:
            valuation = value_movement(
                on_hand_after=after["on_hand_quantity"],
                magnitude=magnitude,
                on_hand_delta=after["on_hand_quantity"] - before["on_hand_quantity"],
                layers=[
                    LayerView(
                        remaining=Decimal(layer.remaining_quantity),
                        unit_cost=Decimal(layer.unit_cost),
                    )
                    for layer in open_layers
                ],
                average_before=Decimal(balance.average_cost),
                movement_unit_cost=movement.unit_cost,
                fallback_cost=item.standard_cost,
                carried=carried,
            )

        occurred_at = movement.occurred_at or utcnow()
        for name in columns:
            setattr(balance, name, to_quantity(after[name]))
        if not from_scrap:
            balance.average_cost = valuation.average_cost
            balance.stock_value = valuation.stock_value
        balance.last_movement_at = occurred_at
        balance.updated_by_id = persisted_actor(self.tenant.user_id)

        # The primary key is assigned here rather than left to the flush,
        # because the cost layers and consumption rows written below carry it as
        # a foreign key and they are staged in the same statement batch. It is
        # the same ``new_id`` the column default would have called, in the same
        # order, so the UUIDv7 monotonicity the FIFO tiebreaker relies on is
        # unchanged.
        transaction_row = InventoryTransaction(
            id=new_id(),
            transaction_type=movement.transaction_type,
            item_id=movement.item_id,
            warehouse_id=movement.warehouse_id,
            location_id=movement.location_id,
            batch_id=movement.batch_id,
            serial_id=movement.serial_id,
            quantity=signed,
            unit_cost=valuation.unit_cost,
            total_cost=valuation.total_cost,
            balance_after=to_quantity(after["on_hand_quantity"]),
            stock_state=movement.stock_state,
            # Stored so the row's effect on on-hand can be recomputed later.
            # ``stock_state`` alone is ambiguous: SCRAP out of the available
            # pool lowers on-hand, SCRAP out of the job-work pool does not, and
            # the two rows are otherwise identical.
            source_state=movement.from_state,
            reference_type=movement.reference_type,
            reference_id=movement.reference_id,
            reference_code=movement.reference_code,
            reason=movement.reason,
            occurred_at=occurred_at,
        )
        self.ledger.add(transaction_row)
        self._write_layers(
            valuation,
            transaction_row,
            balance=balance,
            open_layers=scrap_queue if from_scrap else open_layers,
            occurred_at=occurred_at,
            pool=CostLayerPool.SCRAP if from_scrap else CostLayerPool.ON_HAND,
        )
        if landing_state is StockState.SCRAP:
            # Scrap remembers what it cost. Opened at exactly the costs this
            # ledger row charged, so a later disposal is valued from what the
            # scrap really cost rather than from whatever usable stock happens
            # to stand beside it - and the scrap register's "in" and "out"
            # columns are measured with the same ruler.
            self._write_layers(
                replace(valuation, opened=scrap_layers_for(valuation, magnitude), draws=()),
                transaction_row,
                balance=balance,
                open_layers=scrap_queue,
                occurred_at=occurred_at,
                pool=CostLayerPool.SCRAP,
            )
        if valuation.uncovered_quantity > 0:
            # Reachable only when ``_reconcile_layers`` could not close the gap -
            # it repairs a position whose layers are *short*, and this is what
            # is left if one is short again by the time the movement lands.
            # Logged rather than refused: the stock is physically there, and the
            # audit row below carries the same fact for anyone reconciling.
            log.warning(
                "inventory.fifo_uncovered_quantity",
                item_code=item.code,
                warehouse_code=warehouse.code,
                quantity=str(valuation.uncovered_quantity),
                reference_code=movement.reference_code,
            )
        return _Applied(
            movement=movement,
            transaction=transaction_row,
            serial_change=serial_change,
            # What this movement took out of layers, ready for an inbound leg in
            # the same batch to re-open at its destination.
            carried=tuple(
                LayerCost(quantity=draw.quantity, unit_cost=draw.unit_cost)
                for draw in valuation.draws
            ),
            uncovered_quantity=valuation.uncovered_quantity,
        )

    def _write_layers(
        self,
        valuation: Valuation,
        transaction_row: InventoryTransaction,
        *,
        balance: InventoryBalance,
        open_layers: list[InventoryCostLayer],
        occurred_at: datetime,
        pool: CostLayerPool = CostLayerPool.ON_HAND,
    ) -> None:
        """Open the layers this movement received, and draw down the ones it spent.

        ``open_layers`` is the live FIFO queue of ``pool`` at the position for
        the rest of this batch, so it is kept in the same state the database
        will be in: newly
        opened layers are appended and the list re-sorted on the FIFO key, and
        an exhausted layer is dropped from it - **not** deleted. The row stays,
        with ``remaining_quantity`` at zero: the consumption rows point at it,
        and it is the evidence for what a past issue cost. The partial index
        keeps it out of every future scan.

        ASSUMPTION: every arrival opens a **new** layer; a consumed layer is
        never re-opened. A material return, a customer return and a reversal are
        all arrivals, and putting the quantity back into the layer it came out
        of would do two wrong things: rewrite history that consumption rows and
        costed ledger entries already point at, and put returned goods at the
        *front* of the queue when they are physically the newest thing on the
        shelf. Value is kept whole by the price instead - a document that knows
        what it issued passes that cost back, which is exactly what a production
        return does - and :func:`inbound_unit_cost` records what happens when it
        does not.

        ASSUMPTION: a layer opened at a transfer's destination is dated at the
        *arrival*, not at the original purchase. FIFO is a property of a stock
        position, and at this position the goods arrived today; stock already
        standing here is older and is consumed first. The alternative - carrying
        each layer's original ``received_at`` across the transfer so the company
        consumes by acquisition date wherever the goods are - is defensible too,
        and would matter to a business that transfers stock often enough for the
        two to diverge. What is *not* negotiable, and is preserved here, is the
        order of the carried layers among themselves: they share the arrival
        timestamp and are opened in the order the source gave them up, so the
        UUIDv7 tiebreaker consumes them at the destination in source-FIFO order.
        """
        # Draws first, and only then the new layers: ``LayerDraw.index``
        # addresses ``open_layers`` as the valuation saw it, and appending to
        # the list before spending it would shift every index. A movement never
        # does both - an arrival opens, a departure consumes - but relying on
        # that from two hundred lines away is how the next movement type that
        # does both gets costed against the wrong layers.
        for draw in valuation.draws:
            layer = open_layers[draw.index]
            layer.remaining_quantity = to_quantity(
                Decimal(layer.remaining_quantity) - draw.quantity
            )
            self.cost_layers.touch(layer)
            self.consumptions.add(
                InventoryCostLayerConsumption(
                    transaction_id=transaction_row.id,
                    layer_id=layer.id,
                    quantity=draw.quantity,
                    unit_cost=draw.unit_cost,
                    total_cost=to_money(draw.value),
                )
            )
        if valuation.draws:
            open_layers[:] = [
                layer for layer in open_layers if Decimal(layer.remaining_quantity) > 0
            ]

        for cost in valuation.opened:
            layer = InventoryCostLayer(
                id=new_id(),
                balance_id=balance.id,
                # The grain, copied from the locked balance row rather than from
                # the movement, so a layer can never key on a position other
                # than the one whose stock it accounts for.
                item_id=balance.item_id,
                warehouse_id=balance.warehouse_id,
                location_id=balance.location_id,
                batch_id=balance.batch_id,
                # Business time, so a receipt keyed late is consumed in the
                # order the goods actually arrived.
                received_at=occurred_at,
                original_quantity=cost.quantity,
                remaining_quantity=cost.quantity,
                pool=pool,
                unit_cost=cost.unit_cost,
                source_transaction_id=transaction_row.id,
            )
            self.cost_layers.add(layer)
            open_layers.append(layer)
        if valuation.opened:
            open_layers.sort(key=fifo_sort_key)

    def _decide_serial(
        self,
        movement: MovementRequest,
        master: _MasterData,
        source_state: StockState | None,
        landing_state: StockState | None,
    ) -> tuple[StockSerial | None, SerialStatus | None]:
        """Resolve the unit this movement names and where the movement takes it.

        Raises when the movement is not something that can be done to a unit in
        this state. Writes nothing: the row is only changed once the position
        has been checked too, so a refused posting leaves no half-applied unit
        behind for a later statement to flush.

        The row was locked and re-read by :meth:`_lock_serials`, so the status
        this decides against is the committed one and not a value another
        transaction is in the middle of changing.
        """
        if movement.serial_id is None:
            return None, None

        serial = master.serials.get(movement.serial_id)
        if serial is None:  # pragma: no cover - _validate already refused this
            raise NotFoundError("Stock serial", movement.serial_id)
        if source_state is StockState.JOB_WORK:
            owner = master.job_work_serials.get(serial.id)
            if owner is None or (
                movement.reference_type == "job_work_order"
                and owner != (movement.reference_type, movement.reference_id)
            ):
                raise BusinessRuleError("The serial unit is not held by this job-work order.")

        # A unit belongs to one lot. Naming the other lot would draw from a
        # position the unit is not in and leave both records disagreeing.
        if serial.batch_id is not None and movement.batch_id != serial.batch_id:
            raise BusinessRuleError(
                f"Serial {serial.serial_number} belongs to a different batch "
                f"({serial.batch_id}); name that batch on the movement.",
                details={
                    "serial_number": serial.serial_number,
                    "serial_batch_id": str(serial.batch_id),
                    "movement_batch_id": str(movement.batch_id) if movement.batch_id else None,
                },
            )

        # Where the unit is comes before what the movement does to it: a
        # posting raised at the wrong store is a mis-key, and answering it with
        # a status error would send the storekeeper looking at the unit's
        # lifecycle instead of at the warehouse they typed.
        assert_unit_is_where_the_movement_is(
            serial_number=serial.serial_number,
            transaction_type=movement.transaction_type,
            relocates=movement_relocates_unit(source_state),
            recorded_warehouse_id=serial.warehouse_id,
            movement_warehouse_id=movement.warehouse_id,
            recorded_location_id=serial.location_id,
            movement_location_id=movement.location_id,
        )

        target = serial_transition(
            serial_number=serial.serial_number,
            current=serial.status,
            transaction_type=movement.transaction_type,
            landing_state=landing_state,
            already_received=movement.serial_id in master.received_serials,
            source_state=source_state,
        )
        # A document that names one unit on two receipt lines has to be refused
        # on the second line as well, and the first line's ledger row has not
        # been flushed yet - so the batch remembers what it has just received.
        if target is SerialStatus.IN_STOCK:
            master.received_serials.add(movement.serial_id)
        if movement.transaction_type is TransactionType.JOB_WORK_ISSUE:
            master.job_work_serials[serial.id] = (movement.reference_type, movement.reference_id)
        elif source_state is StockState.JOB_WORK:
            master.job_work_serials.pop(serial.id, None)
        return serial, target

    def _advance_serial(
        self,
        serial: StockSerial,
        target: SerialStatus | None,
        movement: MovementRequest,
        *,
        relocates: bool,
    ) -> _SerialChange | None:
        """Write the decision :meth:`_decide_serial` reached.

        Returns ``None`` when nothing about the unit changed - a QC hold that
        neither moves it nor changes whose hands it is in - so the audit trail
        does not fill with rows recording that nothing happened.
        """
        before = snapshot(serial, _SERIAL_AUDIT_FIELDS)
        if target is not None:
            serial.status = target

        # ASSUMPTION: ``warehouse_id`` and ``location_id`` record where the unit
        # was last put, and only a movement that *puts it somewhere* rewrites
        # them. ``status`` is what says whether it is still there. Splitting the
        # two that way is what makes both columns usable:
        #
        # * A movement that does not relocate the unit leaves the position
        #   alone. Assigning the movement's own warehouse and bin unconditionally
        #   meant a QC hold keyed at warehouse level emptied the bin the unit was
        #   sitting in, and a hold raised against the wrong warehouse silently
        #   teleported the unit there - neither movement moved anything.
        # * A unit that has left keeps the position it left from, rather than
        #   having it cleared. Clearing threw away the answer to "which warehouse
        #   did this unit ship from?", which is the first question a warranty
        #   claim or a recall asks, and it broke the three modules that read this
        #   column to place a unit (assembly, packing and dispatch) - a unit
        #   consumed by one assembly came back as "not held at this warehouse"
        #   instead of "already consumed by AO/...". Liveness is ``status``'s
        #   job, and the engine refuses a second issue on the status alone.
        #
        # NULL therefore keeps one unambiguous meaning: this unit has never been
        # positioned - registered from a packing list, still to arrive - which is
        # what the model's nullable columns are for.
        if relocates:
            serial.warehouse_id = movement.warehouse_id
            serial.location_id = movement.location_id

        after = snapshot(serial, _SERIAL_AUDIT_FIELDS)
        if after == before:
            return None
        self.serials.touch(serial)
        return _SerialChange(serial=serial, before=before, after=after, movement=movement)

    @staticmethod
    def _raise_shortfall(
        shortfall: StockShortfall,
        item: ItemFacts,
        warehouse: WarehouseFacts,
        magnitude: Decimal,
    ) -> None:
        # The one seam every "not enough stock" refusal passes through, so the
        # counter an operator alerts on cannot miss a pool.
        insufficient_stock_events.inc()
        if shortfall.rule in ("available", "on_hand", "reserved"):
            raise InsufficientStockError(
                item_code=item.code,
                warehouse_code=warehouse.code,
                requested=magnitude,
                available=shortfall.available,
            )
        pool = shortfall.rule.removesuffix("_quantity").replace("_", " ")
        raise BusinessRuleError(
            f"Only {shortfall.available} of {item.code} is in {pool} at {warehouse.code}; "
            f"{magnitude} was requested.",
            code=ErrorCode.INSUFFICIENT_STOCK,
            details={
                "item_code": item.code,
                "warehouse_code": warehouse.code,
                "pool": shortfall.rule,
                "requested": str(magnitude),
                "available": str(shortfall.available),
            },
        )

    @staticmethod
    def _traceability_gap(movement: MovementRequest, item: ItemFacts) -> list[str] | None:
        """Which traceability the in-transit exemption let this arrival through without.

        The exemption above :func:`validate_movement` is the right answer - the
        alternative is a stranded lorry-load - but it is not a free one: the
        quantity is booked into a tracked item's position carrying no lot and no
        unit, and nothing else in the system would ever say so. A recall is not
        the moment to discover that.

        So the gap is written into the movement's own audit row, where it is
        indexed by entity and reference and can be listed on demand. It is a
        record, not an alarm: no serial row is invented for goods nobody
        identified, because a fabricated unit is worse than a known gap.
        """
        if not movement.completes_in_transit:
            return None
        gaps = []
        if item.is_batch_tracked and movement.batch_id is None:
            gaps.append("batch")
        if item.is_serial_tracked and movement.serial_id is None:
            gaps.append("serial")
        return gaps or None

    async def _audit(self, step: _Applied, master: _MasterData) -> None:
        movement = step.movement
        txn = step.transaction
        item = master.items[movement.item_id]
        warehouse = master.warehouses[movement.warehouse_id]
        context: dict[str, Any] = {
            "item_code": item.code,
            "warehouse_code": warehouse.code,
            "transaction_type": str(movement.transaction_type),
            "stock_state": str(movement.stock_state),
            "quantity": txn.quantity,
            "balance_after": txn.balance_after,
            "unit_cost": txn.unit_cost,
            "total_cost": txn.total_cost,
            "reference_type": movement.reference_type,
            "reference_id": movement.reference_id,
            "reference_code": movement.reference_code,
            "batch_id": movement.batch_id,
            "serial_id": movement.serial_id,
            "reason": movement.reason,
        }
        # What the movement actually cost, layer by layer. Written into the
        # audit row rather than left implicit in the consumption table, because
        # the question an auditor asks is "why did this issue cost that?" and
        # the answer should be beside the movement, not two joins away.
        if step.carried:
            context["cost_layers_drawn"] = [
                {"quantity": str(cost.quantity), "unit_cost": str(cost.unit_cost)}
                for cost in step.carried
            ]
        if step.uncovered_quantity > 0:
            # The key is *added*, never written as a null, so a JSONB key test
            # selects exactly the movements a person has to look at.
            context["fifo_uncovered_quantity"] = str(step.uncovered_quantity)

        traceability_gap = self._traceability_gap(movement, item)
        if traceability_gap is not None:
            # The key is *added*, never written as a null, so ``context ?
            # 'traceability_gap'`` selects exactly the movements an operator has
            # to reconcile by hand rather than every movement ever posted.
            context["traceability_gap"] = traceability_gap
            log.warning(
                "inventory.traceability_gap",
                item_code=item.code,
                warehouse_code=warehouse.code,
                missing=traceability_gap,
                reference_type=movement.reference_type,
                reference_code=movement.reference_code,
            )
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            action=AuditAction.INVENTORY_MOVEMENT,
            entity_type=AUDIT_ENTITY_MOVEMENT,
            entity_id=txn.id,
            entity_code=movement.reference_code,
            summary=(
                f"{movement.transaction_type} {txn.quantity} {item.code} "
                f"at {warehouse.code} ({movement.reason})"
            ),
            user_id=self.tenant.user_id,
            context=context,
            flush=False,
        )

    async def _audit_reconciliation(self, entry: _LayerReconciliation) -> None:
        """Record a repair of a position whose cost layers did not match its stock.

        Not a stock movement - no quantity changed hands - so it is an update to
        the balance rather than a ledger row. Inventing a movement for it would
        put something in the stock ledger that never happened, and the ledger is
        the one place in the system that may only ever say what did.

        Written for **both** directions. The row is the only durable record that
        a position's valuation basis moved without a document behind it, so
        leaving one direction to a log line would be exactly the silence
        :meth:`_reconcile_layers` exists to avoid.
        """
        queue = "scrap cost layers" if entry.pool is CostLayerPool.SCRAP else "FIFO cost layers"
        if entry.direction == LAYER_RECONCILIATION_EXCESS:
            summary = (
                f"Consumed {entry.quantity} {entry.item_code} of {queue} worth "
                f"{-entry.value} - the layers accounted for stock the position does not hold"
            )
        else:
            summary = (
                f"Opened a reconciliation layer of {entry.quantity} {entry.item_code} "
                f"at {entry.unit_cost} - the position held stock its {queue} did not cover"
            )
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ENTITY_BALANCE,
            entity_id=entry.balance_id,
            entity_code=entry.item_code,
            summary=summary,
            user_id=self.tenant.user_id,
            context={
                "item_id": entry.grain.item_id,
                "warehouse_id": entry.grain.warehouse_id,
                "location_id": entry.grain.location_id,
                "batch_id": entry.grain.batch_id,
                "direction": entry.direction,
                "quantity": entry.quantity,
                "unit_cost": entry.unit_cost,
                # Signed: what the repair added to the position's value, or took
                # out of it. The figure a reconciliation report adds up - for
                # the on-hand queue only; a scrap-queue repair moves no book
                # value, and ``pool`` says which one this was.
                "value_change": entry.value,
                "pool": str(entry.pool),
                "origin": LAYER_ORIGIN_RECONCILIATION,
            },
            flush=False,
        )

    async def _audit_serial(self, change: _SerialChange) -> None:
        """Audit one unit's lifecycle step, in the transaction that caused it.

        A status change is recorded through :func:`record_status_change` like
        every other status column in the system (section 73). A movement that
        only re-positions the unit - a transfer leg, a receipt into a different
        bin - is an update, and is recorded as one with both snapshots so the
        audit says which bin it came from.

        Both snapshots were taken before the flush, so nothing here reads an
        attribute off the ORM row that a flush could have expired.
        """
        serial = change.serial
        from_status = str(change.before["status"])
        to_status = str(change.after["status"])
        movement = change.movement
        # The document that moved it, not just "a movement": tracing a unit that
        # left twice starts from the reference on the offending posting.
        reason = (
            f"{movement.transaction_type} "
            f"{movement.reference_code or movement.reference_type}: {movement.reason}"
        )[:255]

        if from_status != to_status:
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_SERIAL,
                entity_id=serial.id,
                entity_code=serial.serial_number,
                from_status=from_status,
                to_status=to_status,
                user_id=self.tenant.user_id,
                reason=reason,
                flush=False,
            )
            return

        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            action=AuditAction.UPDATE,
            entity_type=AUDIT_ENTITY_SERIAL,
            entity_id=serial.id,
            entity_code=serial.serial_number,
            summary=f"Serial {serial.serial_number} moved position ({reason})",
            user_id=self.tenant.user_id,
            old_values=change.before,
            new_values=change.after,
            flush=False,
        )


# -----------------------------------------------------------------------------
# Public service API - database contract section 4.5
# -----------------------------------------------------------------------------


async def post_movement(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    transaction_type: TransactionType,
    reference_type: str,
    reference_id: uuid.UUID | None,
    reference_code: str | None,
    reason: str,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
    unit_cost: Decimal | None = None,
    stock_state: StockState = StockState.AVAILABLE,
    occurred_at: datetime | None = None,
) -> InventoryTransaction:
    """Post one signed movement. The only way stock changes.

    Locks the balance row, re-reads it, validates, then writes the ledger row,
    the balance and the audit entry in one transaction.
    """
    request = MovementRequest(
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        transaction_type=transaction_type,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_code=reference_code,
        reason=reason,
        location_id=location_id,
        batch_id=batch_id,
        serial_id=serial_id,
        unit_cost=unit_cost,
        stock_state=stock_state,
        occurred_at=occurred_at,
    )
    posted = await MovementEngine(session, tenant).post([request])
    return posted[0]


async def post_movements(
    session: AsyncSession,
    tenant: TenantContext,
    movements: list[MovementRequest],
) -> list[InventoryTransaction]:
    """Batch form: use this for every multi-line document.

    All affected balance rows are locked in primary-key order before any of them
    is changed, so two documents that share items cannot deadlock.
    """
    return await MovementEngine(session, tenant).post(movements)


async def get_balance(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    *,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> InventoryBalance | None:
    """The balance row for one position, or None if it has never held stock."""
    return await InventoryRepository(session, tenant).get_position(
        item_id, warehouse_id, location_id=location_id, batch_id=batch_id
    )


async def get_available(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID | None = None,
) -> Decimal:
    """Sum of ``available_quantity``. Never cached (section 34)."""
    return await InventoryRepository(session, tenant).sum_available(item_id, warehouse_id)


async def transfer_stock(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    from_warehouse_id: uuid.UUID,
    to_warehouse_id: uuid.UUID,
    quantity: Decimal,
    reference_code: str,
    reason: str,
    from_location_id: uuid.UUID | None = None,
    to_location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    unit_cost: Decimal | None = None,
    reference_type: str = "stock_transfer",
    reference_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> tuple[InventoryTransaction, InventoryTransaction]:
    """Immediate two-legged transfer: TRANSFER_OUT then TRANSFER_IN.

    Both legs go through one :func:`post_movements` call so the two positions are
    locked together in a deterministic order. Use this when the stock does not
    spend time in transit - a move between two bays of the same plant. When it
    does, raise a :class:`StockTransfer` document instead, which tracks the
    in-transit quantity at the destination while the goods travel.
    """
    if from_warehouse_id == to_warehouse_id:
        raise BusinessRuleError("Source and destination warehouses must differ.")

    engine = MovementEngine(session, tenant)
    async with transaction(session):
        source_grain = BalanceGrain(
            item_id=item_id,
            warehouse_id=from_warehouse_id,
            location_id=from_location_id,
            batch_id=batch_id,
        )
        destination_grain = BalanceGrain(
            item_id=item_id,
            warehouse_id=to_warehouse_id,
            location_id=to_location_id,
            batch_id=batch_id,
        )
        # Both positions are checked before either is locked. ``lock`` creates
        # the balance row it locks, so a destination bin belonging to another
        # tenant - or to a third warehouse - would be created here, one
        # statement before the movement validation that would have refused it.
        await engine.validate_positions(
            [source_grain, destination_grain], receiving=[destination_grain]
        )
        # Both positions are locked in one ordered acquisition. Posting the legs
        # in two calls would take the two warehouses' locks in opposite orders
        # for a transfer running the other way, which is a deadlock.
        locked = await engine.lock([source_grain, destination_grain])

        # The stock carries its value with it, layer by layer. The outbound
        # leg consumes the source's cost layers oldest first; ``carries_cost_of``
        # re-opens *those same costs* at the destination rather than one layer
        # at a blended price. A blend would be wrong twice: it rounds a
        # multi-layer draw to six places, so moving a box changes what the
        # company's stock is worth, and it destroys the order the destination
        # will consume it in - 20 @ 100 and 10 @ 120 becoming 30 @ 106.666667
        # means the next issue there is costed from a price that was never paid.
        #
        # ``unit_cost`` is the price for any quantity the source's layers cannot
        # account for, not an override of what they say. An outbound movement is
        # never priced by its caller.
        common: dict[str, Any] = {
            "item_id": item_id,
            "quantity": quantity,
            "reference_type": reference_type,
            "reference_id": reference_id,
            "reference_code": reference_code,
            "reason": reason,
            "batch_id": batch_id,
            "unit_cost": unit_cost,
            "occurred_at": occurred_at,
        }
        out_leg = MovementRequest(
            warehouse_id=from_warehouse_id,
            location_id=from_location_id,
            transaction_type=TransactionType.TRANSFER_OUT,
            **common,
        )
        in_leg = MovementRequest(
            warehouse_id=to_warehouse_id,
            location_id=to_location_id,
            transaction_type=TransactionType.TRANSFER_IN,
            carries_cost_of=0,
            **common,
        )
        posted, _ = await engine.post_with_balances([out_leg, in_leg], prelocked=locked)
    return posted[0], posted[1]


async def change_stock_state(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    from_state: StockState,
    to_state: StockState,
    reason: str,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
    reference_type: str = "stock_state_change",
    reference_id: uuid.UUID | None = None,
    reference_code: str | None = None,
    occurred_at: datetime | None = None,
) -> InventoryTransaction:
    """Move a quantity between two stock states at one position.

    QC hold to available, available to damaged, damaged to scrap, and so on. The
    quantity does not leave the warehouse unless the target state says it does
    (scrap and job work), which is why this is one ledger row rather than a pair.
    """
    transaction_type = state_change_type(from_state, to_state)
    request = MovementRequest(
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        transaction_type=transaction_type,
        reference_type=reference_type,
        reference_id=reference_id,
        reference_code=reference_code,
        reason=f"{from_state} -> {to_state}: {reason}"[:255],
        location_id=location_id,
        batch_id=batch_id,
        serial_id=serial_id,
        stock_state=to_state,
        from_state=from_state,
        occurred_at=occurred_at,
    )
    posted = await MovementEngine(session, tenant).post([request])
    return posted[0]


# -----------------------------------------------------------------------------
# Read services
# -----------------------------------------------------------------------------


class InventoryQueryService:
    """Balance, ledger and summary reads. No writes, no locks."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.balances = InventoryRepository(session, tenant)
        self.ledger = LedgerRepository(session, tenant)

    async def list_balances(
        self, filters: BalanceFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[BalanceReadWithCost], OffsetPagination]:
        """Every field the row has, cost included.

        The *widest* model on purpose: the caller decides what a given principal
        may see, and a service that dropped the cost here would leave the route
        unable to serve it to somebody entitled to it.
        """
        stmt = self.balances.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)

        items = await self.balances.item_facts([row.item_id for row in rows])
        warehouses = await self.balances.warehouse_facts([row.warehouse_id for row in rows])
        batch_numbers = await self.balances.batch_numbers(
            [row.batch_id for row in rows if row.batch_id]
        )
        locations = await self.balances.location_facts(
            [row.location_id for row in rows if row.location_id]
        )

        results: list[BalanceReadWithCost] = []
        for row in rows:
            item = items.get(row.item_id)
            warehouse = warehouses.get(row.warehouse_id)
            read = BalanceReadWithCost.model_validate(row)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            read.reorder_level = item.reorder_level if item else None
            read.warehouse_code = warehouse.code if warehouse else None
            read.batch_number = batch_numbers.get(row.batch_id) if row.batch_id else None
            location = locations.get(row.location_id) if row.location_id else None
            read.location_code = location.code if location else None
            results.append(read)
        return results, pagination

    async def list_ledger(
        self, filters: LedgerFilters, params: CursorParams
    ) -> tuple[list[LedgerEntryRead], CursorPagination]:
        """Cursor paginated: this table only ever grows (section 31)."""
        stmt = self.ledger.build_list_stmt(filters)
        rows, pagination = await paginate_keyset(
            self.session,
            stmt,
            params,
            created_at_col=InventoryTransaction.created_at,
            id_col=InventoryTransaction.id,
        )

        items = await self.balances.item_facts([row.item_id for row in rows])
        warehouses = await self.balances.warehouse_facts([row.warehouse_id for row in rows])

        results: list[LedgerEntryRead] = []
        for row in rows:
            item = items.get(row.item_id)
            warehouse = warehouses.get(row.warehouse_id)
            entry = LedgerEntryRead.model_validate(row)
            entry.item_code = item.code if item else None
            entry.item_name = item.name if item else None
            entry.warehouse_code = warehouse.code if warehouse else None
            results.append(entry)
        return results, pagination

    async def item_summary(
        self, item_id: uuid.UUID, params: PageParams
    ) -> tuple[ItemStockSummaryWithCost, OffsetPagination]:
        """One item's stock across warehouses, with company-wide totals.

        Built with the value included; the route narrows it away for a caller
        without ``inventory.valuation_view``.

        The totals are computed over the whole item, not over the current page -
        a planner reading "total available" must not see a number that changes
        when they turn the page.
        """
        items = await self.balances.item_facts([item_id])
        item = items.get(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)

        rows, pagination = await self.balances.warehouse_summary(item_id, params)
        totals = await self.balances.item_totals(item_id)

        summary = ItemStockSummaryWithCost(
            item_id=item_id,
            item_code=item.code,
            item_name=item.name,
            reorder_level=item.reorder_level,
            total_on_hand=totals["on_hand_quantity"],
            total_reserved=totals["reserved_quantity"],
            total_available=totals["available_quantity"],
            total_in_transit=totals["in_transit_quantity"],
            total_job_work=totals["job_work_quantity"],
            total_scrap=totals["scrap_quantity"],
            # Scrap is the company's until it is disposed of, but it is not
            # stock anybody can use or plan with, so it is reported beside the
            # owned total rather than inside it.
            total_owned_quantity=(
                totals["on_hand_quantity"]
                + totals["in_transit_quantity"]
                + totals["job_work_quantity"]
            ),
            total_value=totals["stock_value"],
            warehouses=[
                WarehouseStockRowWithCost(
                    warehouse_id=row.warehouse_id,
                    warehouse_code=row.warehouse_code,
                    warehouse_name=row.warehouse_name,
                    on_hand_quantity=row.on_hand_quantity,
                    reserved_quantity=row.reserved_quantity,
                    available_quantity=row.available_quantity,
                    in_transit_quantity=row.in_transit_quantity,
                    qc_hold_quantity=row.qc_hold_quantity,
                    damaged_quantity=row.damaged_quantity,
                    job_work_quantity=row.job_work_quantity,
                    scrap_quantity=row.scrap_quantity,
                    stock_value=row.stock_value,
                )
                for row in rows
            ],
        )
        return summary, pagination

    async def availability(
        self, item_id: uuid.UUID, warehouse_id: uuid.UUID | None
    ) -> AvailabilityRead:
        quantity = await self.balances.sum_available(item_id, warehouse_id)
        return AvailabilityRead(
            item_id=item_id, warehouse_id=warehouse_id, available_quantity=quantity
        )


# -----------------------------------------------------------------------------
# Traceability service
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpirySweepResult:
    expired_batch_ids: tuple[uuid.UUID, ...]
    warning_batch_ids: tuple[uuid.UUID, ...]


class TraceabilityService:
    """Batches and serial numbers.

    Both exist before any stock does: a batch-tracked item cannot be received
    until the lot it arrived in has been recorded, because the engine refuses a
    movement without one.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.batches = StockBatchRepository(session, tenant)
        self.serials = StockSerialRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)

    async def create_batch(self, payload: StockBatchCreate) -> StockBatchRead:
        async with transaction(self.session):
            items = await self.inventory.item_facts([payload.item_id])
            item = items.get(payload.item_id)
            if item is None:
                raise NotFoundError("Item", payload.item_id)
            if await self.batches.by_number(payload.item_id, payload.batch_number) is not None:
                raise DuplicateError(
                    f"Batch {payload.batch_number} already exists for item {item.code}.",
                    details={"batch_number": payload.batch_number},
                )

            batch = StockBatch(
                item_id=payload.item_id,
                batch_number=payload.batch_number,
                supplier_batch_number=payload.supplier_batch_number,
                vendor_id=payload.vendor_id,
                manufactured_on=payload.manufactured_on,
                expires_on=payload.expires_on,
                received_on=payload.received_on or today(),
                initial_quantity=payload.initial_quantity,
                unit_cost=payload.unit_cost,
                notes=payload.notes,
            )
            self.batches.add(batch)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_BATCH,
                entity_id=batch.id,
                entity_code=batch.batch_number,
                summary=f"Created batch {batch.batch_number} for {item.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    batch, ["item_id", "batch_number", "expires_on", "unit_cost", "status"]
                ),
            )
        return (await self._batch_reads([batch]))[0]

    async def get_batch(self, batch_id: uuid.UUID) -> StockBatchRead:
        batch = await self.batches.get(batch_id)
        if batch is None:
            raise NotFoundError("Stock batch", batch_id)
        return (await self._batch_reads([batch]))[0]

    async def list_batches(
        self,
        item_id: uuid.UUID | None,
        search: str | None,
        params: PageParams,
        *,
        batch_id: uuid.UUID | None = None,
        status: BatchStatus | None = None,
        expiry_from: date | None = None,
        expiry_to: date | None = None,
    ) -> tuple[list[StockBatchRead], OffsetPagination]:
        stmt = self.batches.build_list_stmt(
            item_id=item_id,
            search=search,
            batch_id=batch_id,
            status=status,
            expiry_from=expiry_from,
            expiry_to=expiry_to,
        )
        rows, pagination = await paginate(self.session, stmt, params)
        return await self._batch_reads(rows), pagination

    async def _batch_reads(self, rows: Sequence[StockBatch]) -> list[StockBatchRead]:
        """Attach the labels and current quantities a tracker row needs."""
        if not rows:
            return []
        items = await self.inventory.item_facts([row.item_id for row in rows])
        totals = await self.inventory.batch_totals([row.id for row in rows])
        reads: list[StockBatchRead] = []
        for row in rows:
            read = StockBatchRead.model_validate(row)
            item = items.get(row.item_id)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            quantity = totals.get(row.id)
            if quantity is not None:
                read.on_hand_quantity = quantity.on_hand
                read.available_quantity = quantity.available
                read.in_transit_quantity = quantity.in_transit
            reads.append(read)
        return reads

    async def create_serial(self, payload: StockSerialCreate) -> StockSerialRead:
        async with transaction(self.session):
            items = await self.inventory.item_facts([payload.item_id])
            item = items.get(payload.item_id)
            if item is None:
                raise NotFoundError("Item", payload.item_id)
            if not item.is_serial_tracked:
                raise BusinessRuleError(
                    f"{item.code} is not serial tracked, so it has no serial numbers to record.",
                    details={"item_code": item.code, "item_id": str(item.id)},
                )
            existing = await self.serials.by_number(payload.item_id, payload.serial_number)
            if existing is not None:
                raise DuplicateError(
                    f"Serial {payload.serial_number} already exists for item {item.code}.",
                    details={"serial_number": payload.serial_number},
                )
            if payload.batch_id is not None:
                owners = await self.inventory.batch_owners([payload.batch_id])
                owner = owners.get(payload.batch_id)
                if owner is None:
                    raise NotFoundError("Stock batch", payload.batch_id)
                if owner[0] != payload.item_id:
                    raise BusinessRuleError("That batch belongs to a different item.")

            # A registration may say where the unit is expected to land, and
            # that position has to be real and ours. The foreign keys on
            # stock_serials are company-blind, exactly like the one on a
            # balance row, so without this a unit can be registered against
            # another tenant's bin - the same phantom position a movement
            # naming one would create, written a day earlier.
            if payload.warehouse_id is not None:
                warehouses = await self.inventory.warehouse_facts([payload.warehouse_id])
                if payload.warehouse_id not in warehouses:
                    raise NotFoundError("Warehouse", payload.warehouse_id)
            if payload.location_id is not None:
                locations = await self.inventory.location_facts([payload.location_id])
                location = locations.get(payload.location_id)
                if location is None:
                    raise NotFoundError("Storage location", payload.location_id)
                # ASSUMPTION: a bin named without a warehouse is accepted as
                # given rather than used to infer one. The bin is real and
                # belongs to this tenant, which is what the phantom-position
                # rule is about; inferring the warehouse from it would be this
                # service deciding a fact the caller did not state.
                if (
                    payload.warehouse_id is not None
                    and location.warehouse_id != payload.warehouse_id
                ):
                    raise BusinessRuleError(
                        f"Storage location {location.code} is not in that warehouse.",
                        details={
                            "location_id": str(payload.location_id),
                            "warehouse_id": str(payload.warehouse_id),
                        },
                    )

            serial = StockSerial(
                item_id=payload.item_id,
                serial_number=payload.serial_number,
                batch_id=payload.batch_id,
                warehouse_id=payload.warehouse_id,
                location_id=payload.location_id,
                received_on=payload.received_on or today(),
                unit_cost=payload.unit_cost,
                notes=payload.notes,
            )
            self.serials.add(serial)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_SERIAL,
                entity_id=serial.id,
                entity_code=serial.serial_number,
                summary=f"Created serial {serial.serial_number} for {item.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(serial, ["item_id", "serial_number", "status", "unit_cost"]),
            )
        return (await self._serial_reads([serial]))[0]

    async def bulk_create_serials(self, payload: StockSerialBulkCreate) -> list[StockSerialRead]:
        """Register a label run in one transaction.

        This is the only bulk entry point into the serial register, and it is
        capped at :data:`MAX_SERIAL_BULK`. Anything larger belongs on the goods
        receipt that actually brought the units in, where each serial is tied to
        a purchase order line and a batch.
        """
        async with transaction(self.session):
            items = await self.inventory.item_facts([payload.item_id])
            item = items.get(payload.item_id)
            if item is None:
                raise NotFoundError("Item", payload.item_id)
            if not item.is_serial_tracked:
                raise BusinessRuleError(
                    f"{item.code} is not serial tracked, so it has no serial numbers to record.",
                    details={"item_code": item.code, "item_id": str(item.id)},
                )
            await self._validate_serial_position(
                item_id=payload.item_id,
                batch_id=payload.batch_id,
                warehouse_id=payload.warehouse_id,
                location_id=payload.location_id,
            )
            existing = await self.serials.by_numbers(payload.item_id, payload.serial_numbers)
            if existing:
                duplicate = next(iter(existing))
                raise DuplicateError(
                    f"Serial {duplicate} already exists for item {item.code}.",
                    details={"serial_number": duplicate, "duplicates": sorted(existing)},
                )
            serials = [
                StockSerial(
                    item_id=payload.item_id,
                    serial_number=serial_number,
                    batch_id=payload.batch_id,
                    warehouse_id=payload.warehouse_id,
                    location_id=payload.location_id,
                    received_on=payload.received_on or today(),
                    unit_cost=payload.unit_cost,
                    notes=payload.notes,
                )
                for serial_number in payload.serial_numbers
            ]
            for serial in serials:
                self.serials.add(serial)
            await self.session.flush()
            for serial in serials:
                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CREATE,
                    entity_type=AUDIT_ENTITY_SERIAL,
                    entity_id=serial.id,
                    entity_code=serial.serial_number,
                    summary=f"Created serial {serial.serial_number} for {item.code}",
                    user_id=self.tenant.user_id,
                    new_values=snapshot(
                        serial, ["item_id", "serial_number", "status", "unit_cost"]
                    ),
                )
            await self.session.flush()
            return await self._serial_reads(serials)

    async def resolve_serial_ids(
        self,
        item_id: uuid.UUID,
        serial_numbers: Sequence[str],
        *,
        batch_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
        received_on: date | None,
        unit_cost: Decimal = Decimal("0"),
        source: str,
    ) -> list[uuid.UUID]:
        """Adopt pre-registered units and mint the rest, preserving input order.

        A serial row with no receipt movement is a label printed ahead of the
        goods; one with a receipt movement is a unit already on the shelf and a
        second receipt of it is a duplicate. That distinction is the ledger's,
        not the status column's, because both rows are ``IN_STOCK``.
        """
        items = await self.inventory.item_facts([item_id])
        item = items.get(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)
        if not item.is_serial_tracked:
            raise BusinessRuleError(
                f"{item.code} is not serial tracked; remove the serial numbers.",
                details={"item_code": item.code, "source": source},
            )
        cleaned = ["".join(value.split()).upper() for value in serial_numbers]
        if len(set(cleaned)) != len(cleaned):
            raise BusinessRuleError(
                "A document cannot name the same serial twice.",
                details={"item_code": item.code, "source": source},
            )
        existing = await self.serials.by_numbers(item_id, cleaned)
        already_received = await LedgerRepository(self.session, self.tenant).received_serial_ids(
            [serial.id for serial in existing.values()]
        )
        if already_received:
            by_id = {serial.id: serial.serial_number for serial in existing.values()}
            duplicate = by_id[next(iter(already_received))]
            raise DuplicateError(
                f"Serial {duplicate} has already been received.",
                details={"serial_number": duplicate, "item_code": item.code, "source": source},
            )
        for serial in existing.values():
            if SerialStatus(serial.status) is not SerialStatus.IN_STOCK:
                raise BusinessRuleError(
                    f"Serial {serial.serial_number} is {serial.status} and cannot be received.",
                    details={"serial_number": serial.serial_number, "status": str(serial.status)},
                )
            if serial.batch_id != batch_id:
                if serial.batch_id is None and batch_id is not None:
                    serial.batch_id = batch_id
                else:
                    raise BusinessRuleError(
                        f"Serial {serial.serial_number} is registered to a different batch.",
                        details={
                            "serial_number": serial.serial_number,
                            "batch_id": str(serial.batch_id) if serial.batch_id else None,
                        },
                    )
        missing = [serial_number for serial_number in cleaned if serial_number not in existing]
        if missing:
            await self.bulk_create_serials(
                StockSerialBulkCreate(
                    item_id=item_id,
                    serial_numbers=missing,
                    batch_id=batch_id,
                    warehouse_id=warehouse_id,
                    location_id=location_id,
                    received_on=received_on,
                    unit_cost=unit_cost,
                )
            )
        after = await self.serials.by_numbers(item_id, cleaned)
        absent = [serial_number for serial_number in cleaned if serial_number not in after]
        if absent:
            raise NotFoundError("Stock serial", absent[0])
        return [after[number].id for number in cleaned]

    async def _validate_serial_position(
        self,
        *,
        item_id: uuid.UUID,
        batch_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        location_id: uuid.UUID | None,
    ) -> None:
        if batch_id is not None:
            facts = await self.inventory.batch_facts([batch_id])
            owner = facts.get(batch_id)
            if owner is None:
                raise NotFoundError("Stock batch", batch_id)
            if owner.item_id != item_id:
                raise BusinessRuleError("That batch belongs to a different item.")
        if warehouse_id is not None:
            warehouses = await self.inventory.warehouse_facts([warehouse_id])
            if warehouse_id not in warehouses:
                raise NotFoundError("Warehouse", warehouse_id)
        if location_id is not None:
            locations = await self.inventory.location_facts([location_id])
            location = locations.get(location_id)
            if location is None:
                raise NotFoundError("Storage location", location_id)
            if warehouse_id is not None and location.warehouse_id != warehouse_id:
                raise BusinessRuleError(
                    f"Storage location {location.code} is not in that warehouse.",
                    details={
                        "location_id": str(location_id),
                        "warehouse_id": str(warehouse_id),
                    },
                )

    async def list_serials(
        self,
        item_id: uuid.UUID | None,
        warehouse_id: uuid.UUID | None,
        search: str | None,
        params: PageParams,
        *,
        status: SerialStatus | None = None,
        batch_id: uuid.UUID | None = None,
        serial_ids: list[uuid.UUID] | None = None,
        job_work_order_line_id: uuid.UUID | None = None,
        job_work_outstanding_only: bool = False,
        qc_inspection_id: uuid.UUID | None = None,
    ) -> tuple[list[StockSerialRead], OffsetPagination]:
        if job_work_outstanding_only and job_work_order_line_id is None:
            raise ValidationError("Select a job-work line to list its outstanding serial units.")
        stmt = self.serials.build_list_stmt(
            item_id=item_id,
            warehouse_id=warehouse_id,
            search=search,
            status=status,
            batch_id=batch_id,
            serial_ids=serial_ids,
            job_work_order_line_id=job_work_order_line_id,
            job_work_outstanding_only=job_work_outstanding_only,
            qc_inspection_id=qc_inspection_id,
        )
        rows, pagination = await paginate(self.session, stmt, params)
        return await self._serial_reads(rows), pagination

    async def _serial_reads(self, rows: Sequence[StockSerial]) -> list[StockSerialRead]:
        if not rows:
            return []
        items = await self.inventory.item_facts([row.item_id for row in rows])
        batch_ids = [row.batch_id for row in rows if row.batch_id is not None]
        batches = await self.inventory.batch_facts(batch_ids) if batch_ids else {}
        warehouse_ids = [row.warehouse_id for row in rows if row.warehouse_id is not None]
        warehouses = await self.inventory.warehouse_facts(warehouse_ids) if warehouse_ids else {}
        reads: list[StockSerialRead] = []
        for row in rows:
            read = StockSerialRead.model_validate(row)
            item = items.get(row.item_id)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            if row.batch_id is not None and row.batch_id in batches:
                read.batch_number = batches[row.batch_id].batch_number
            if row.warehouse_id is not None:
                warehouse = warehouses.get(row.warehouse_id)
                read.warehouse_code = warehouse.code if warehouse else None
            reads.append(read)
        return reads

    async def get_serial(self, serial_id: uuid.UUID) -> StockSerialDetail:
        serial = await self.serials.get(serial_id)
        if serial is None:
            raise NotFoundError("Stock serial", serial_id)
        read = (await self._serial_reads([serial]))[0]
        ledger = LedgerRepository(self.session, self.tenant)
        family = await ledger.serial_family(serial_id)
        if len(family) > 500:
            raise BusinessRuleError("This serial history exceeds the interactive trace limit.")
        movements = await ledger.for_serial(serial_id, related_ids=family)
        conversions = (
            await self.session.scalars(
                select(StockSerialConversion)
                .where(
                    StockSerialConversion.company_id == self.tenant.company_id,
                    StockSerialConversion.source_serial_id.in_(family),
                )
                .order_by(StockSerialConversion.created_at, StockSerialConversion.id)
            )
        ).all()
        items = await self.inventory.item_facts(
            [serial.item_id, *(row.item_id for row in movements)]
        )
        warehouses = await self.inventory.warehouse_facts(
            [movement.warehouse_id for movement in movements]
        )
        entries: list[LedgerEntryRead] = []
        for movement in movements:
            entry = LedgerEntryRead.model_validate(movement)
            item = items.get(movement.item_id)
            entry.item_code = item.code if item else None
            entry.item_name = item.name if item else None
            warehouse = warehouses.get(movement.warehouse_id)
            entry.warehouse_code = warehouse.code if warehouse else None
            entries.append(entry)
        return StockSerialDetail(
            serial=read,
            movements=entries,
            conversions=[StockSerialConversionRead.model_validate(row) for row in conversions],
        )

    async def quarantine_batch(
        self, batch_id: uuid.UUID, payload: StockBatchAction
    ) -> StockBatchRead:
        async with transaction(self.session):
            batch = await self._lock_batch(batch_id)
            previous = BatchStatus(batch.status)
            BATCH_FLOW.assert_transition(previous, BatchStatus.QUARANTINED)
            positions = await self.inventory.batch_available_positions(batch_id)
            movements = await self._quarantine_movements(batch, positions, payload.reason)
            batch.status = BatchStatus.QUARANTINED
            self.batches.touch(batch)
            if movements:
                await MovementEngine(self.session, self.tenant).post(movements)
            await self._record_batch_transition(batch, previous, payload.reason)
        return await self.get_batch(batch_id)

    async def _quarantine_movements(
        self,
        batch: StockBatch,
        positions: Sequence[InventoryBalance],
        reason: str,
    ) -> list[MovementRequest]:
        """One movement per position, and one per unit where it is serialised.

        The engine refuses a quantity other than one against a serialised item,
        so a lot-level hold has to name the individual units. Their count must
        equal the position's AVAILABLE quantity: a serial row records that a
        unit is in stock, not which pool it is in, so a shortfall means some
        units of this lot are already held elsewhere and moving the rest by
        count would take the wrong ones. Refusing is the only safe answer.
        """
        if not positions:
            return []
        items = await self.inventory.item_facts([position.item_id for position in positions])
        units_by_position: dict[tuple[uuid.UUID | None, uuid.UUID | None], list[StockSerial]] = {}
        for unit in await self.serials.in_stock_for_batch(batch.id):
            units_by_position.setdefault((unit.warehouse_id, unit.location_id), []).append(unit)

        movements: list[MovementRequest] = []
        for position in positions:
            item = items.get(position.item_id)
            if item is None:
                raise NotFoundError("Item", position.item_id)
            common: dict[str, Any] = {
                "item_id": position.item_id,
                "warehouse_id": position.warehouse_id,
                "location_id": position.location_id,
                "batch_id": batch.id,
                "transaction_type": TransactionType.QC_HOLD,
                "reference_type": AUDIT_ENTITY_BATCH,
                "reference_id": batch.id,
                "reference_code": batch.batch_number,
                "reason": reason,
                "stock_state": StockState.QC_HOLD,
                "from_state": StockState.AVAILABLE,
            }
            quantity = Decimal(position.available_quantity)
            if item.is_serial_tracked:
                units = units_by_position.get((position.warehouse_id, position.location_id), [])
                if Decimal(len(units)) != quantity:
                    raise BusinessRuleError(
                        f"Lot {batch.batch_number} has {quantity} available unit(s) but "
                        f"{len(units)} in-stock serial(s) in this position. Reconcile the "
                        "serial register before quarantining the lot.",
                        details={
                            "batch_id": str(batch.id),
                            "warehouse_id": str(position.warehouse_id),
                            "location_id": (
                                str(position.location_id)
                                if position.location_id is not None
                                else None
                            ),
                            "available_quantity": str(quantity),
                            "in_stock_serials": len(units),
                        },
                    )
                movements.extend(
                    MovementRequest(quantity=Decimal("1"), serial_id=unit.id, **common)
                    for unit in units
                )
            else:
                movements.append(MovementRequest(quantity=quantity, **common))
        return movements

    async def activate_batch(
        self, batch_id: uuid.UUID, payload: StockBatchAction
    ) -> StockBatchRead:
        """Release a quarantined lot's status.

        Stock already sitting in QC hold stays there: releasing a lot from
        quarantine is a quality decision about the lot, while releasing stock
        from a hold is a warehouse movement with its own record. Doing both here
        would hide a stock movement inside a status button.
        """
        async with transaction(self.session):
            batch = await self._lock_batch(batch_id)
            previous = BatchStatus(batch.status)
            BATCH_FLOW.assert_transition(previous, BatchStatus.ACTIVE)
            batch.status = BatchStatus.ACTIVE
            self.batches.touch(batch)
            await self._record_batch_transition(batch, previous, payload.reason)
        return await self.get_batch(batch_id)

    async def expire_batch(self, batch_id: uuid.UUID, payload: StockBatchAction) -> StockBatchRead:
        async with transaction(self.session):
            batch = await self._lock_batch(batch_id)
            await self._expire(batch, payload.reason)
        return await self.get_batch(batch_id)

    async def _lock_batch(self, batch_id: uuid.UUID) -> StockBatch:
        batch = await self.batches.lock_for_update(batch_id)
        if batch is None:
            raise NotFoundError("Stock batch", batch_id)
        return batch

    async def _expire(self, batch: StockBatch, reason: str) -> None:
        previous = BatchStatus(batch.status)
        BATCH_FLOW.assert_transition(previous, BatchStatus.EXPIRED)
        batch.status = BatchStatus.EXPIRED
        self.batches.touch(batch)
        await self._record_batch_transition(batch, previous, reason)

    async def _record_batch_transition(
        self, batch: StockBatch, previous: BatchStatus, reason: str
    ) -> None:
        await self.session.flush()
        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ENTITY_BATCH,
            entity_id=batch.id,
            entity_code=batch.batch_number,
            from_status=str(previous),
            to_status=str(batch.status),
            user_id=self.tenant.user_id,
            reason=reason,
        )

    async def sweep_expiry(self, *, warn_days: int = BATCH_EXPIRY_WARN_DAYS) -> ExpirySweepResult:
        """Expire lots whose date has passed and warn about the rest.

        One query, one digest per company per day. The notification is a
        ``STOCK_EXPIRY`` alert to the roles that can act on it rather than one
        notification per lot, which would bury the bell under a warehouse's
        worth of near-expiry labels.
        """
        now = today()
        batches = await self.batches.expiring(
            as_of=now, warn_before=now + timedelta(days=warn_days)
        )
        expired: list[uuid.UUID] = []
        warning: list[uuid.UUID] = []
        for batch in batches:
            if batch.expires_on is not None and batch.expires_on < now:
                await self._expire(
                    batch,
                    f"Expired on {batch.expires_on.isoformat()} (automatic sweep).",
                )
                expired.append(batch.id)
            else:
                warning.append(batch.id)
        await self.session.flush()
        if expired or warning:
            from src.modules.notifications.models import NotificationSeverity, NotificationType
            from src.modules.notifications.service import notify_role

            body = (
                f"{len(expired)} lot(s) expired and {len(warning)} expire within {warn_days} days."
            )
            await notify_role(
                self.session,
                company_id=self.tenant.company_id,
                role_codes=("STORE_KEEPER", "QC_INSPECTOR", "PLANT_MANAGER", "ADMIN"),
                notification_type=NotificationType.STOCK_EXPIRY,
                title="Batch expiry review",
                body=body,
                severity=NotificationSeverity.WARNING,
                entity_type=AUDIT_ENTITY_BATCH,
                entity_id=(expired + warning)[0],
                action_url="/inventory/tracking?tab=batches",
                group_key=f"stock-expiry:{now.isoformat()}",
                actor_id=self.tenant.user_id,
            )
        return ExpirySweepResult(expired_batch_ids=tuple(expired), warning_batch_ids=tuple(warning))


def _declare_lines_empty_if_unloaded(row: Any) -> None:
    """Make a list row's `lines` readable without loading it.

    `StockTransferRead` and `StockAdjustmentRead` both declare a `lines` field,
    so `model_validate(row)` reads the relationship - and the models declare it
    `lazy="raise"`. On a list row that is unloaded, so validation raised
    `InvalidRequestError` and both endpoints answered 500 for every row they
    found; they were clean only while the table was empty, which is every
    automated run, because the demo data raises neither document. Clearing
    `read.lines` afterwards cannot help: the read happens inside validation.

    Declaring it loaded and empty first costs no query. The `unloaded` guard is
    the load-bearing half: the detail, create and post paths return through the
    same builder with the collection genuinely loaded, and overwriting it there
    would blank the lines those responses are expected to carry.
    """
    if "lines" in sa_inspect(row).unloaded:
        set_committed_value(row, "lines", [])


# -----------------------------------------------------------------------------
# Stock transfer service
# -----------------------------------------------------------------------------


def carried_cost_layers(
    breakdown: dict[uuid.UUID, list[InventoryCostLayerConsumption]],
    transaction_id: uuid.UUID | None,
) -> tuple[LayerCost, ...]:
    """The cost layers one dispatched line consumed, ready to be re-opened.

    Empty when the line has no dispatch movement recorded - a transfer that left
    before FIFO valuation landed - in which case the arrival falls back to the
    blended ``unit_cost`` stored on the line. That is a worse answer than the
    breakdown and a much better one than refusing the arrival: there is no edge
    out of IN_TRANSIT except RECEIVED, so a refusal would strand the goods.
    """
    if transaction_id is None:
        return ()
    return tuple(
        LayerCost(quantity=Decimal(row.quantity), unit_cost=Decimal(row.unit_cost))
        for row in breakdown.get(transaction_id, ())
    )


async def _adopt_named_serials(
    session: AsyncSession,
    tenant: TenantContext,
    lines: Sequence[StockTransferLineCreate | StockAdjustmentLineCreate],
    items: dict[uuid.UUID, ItemFacts],
) -> None:
    """Turn typed or scanned labels on document lines into serial ids.

    The stock screens capture the label printed on the unit because that is what
    the operator can read; the ledger keys the immutable serial id. Resolving
    here, in one query for the whole document, keeps that translation out of the
    UI without putting a lookup inside a line loop. Only already-registered
    units are adopted: a transfer or adjustment moves stock that exists, and
    minting a label on this path would create a unit with nothing behind it.
    """
    labelled = [line for line in lines if line.serial_numbers]
    if not labelled:
        return

    pairs = [
        (line.item_id, serial_number) for line in labelled for serial_number in line.serial_numbers
    ]
    serials = await StockSerialRepository(session, tenant).by_number_pairs(pairs)

    for number, line in enumerate(lines, start=1):
        if not line.serial_numbers:
            continue
        item = items[line.item_id]
        if not item.is_serial_tracked:
            raise BusinessRuleError(
                f"Line {number}: {item.code} is not serial tracked; remove the serials.",
                details={"line_number": number, "item_code": item.code},
            )
        resolved: list[uuid.UUID] = []
        for serial_number in line.serial_numbers:
            serial = serials.get((line.item_id, serial_number))
            if serial is None:
                raise BusinessRuleError(
                    f"Line {number}: serial {serial_number} is not registered for {item.code}.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={
                        "line_number": number,
                        "serial_number": serial_number,
                        "item_code": item.code,
                    },
                )
            resolved.append(serial.id)
        line.serial_ids = resolved

    # The request validators can prove ids are unique, or labels are unique, but
    # not that a label on one line names the same unit as an id on another. Now
    # that both have become ids, check the whole document before it can move a
    # unit twice.
    moved = [serial_id for line in lines for serial_id in line.serial_ids]
    if len(set(moved)) != len(moved):
        raise BusinessRuleError("The same unit is named on more than one line of this document.")


class StockTransferService:
    """The stock transfer document lifecycle.

    Dispatching posts ``TRANSFER_OUT`` at the source and raises the destination's
    ``in_transit_quantity``; receiving posts ``TRANSFER_IN`` at the destination
    and lowers it again. Between the two the stock is on a vehicle: absent from
    both warehouses' on-hand, present in the company's total ownership, and
    visible to anyone reading the destination's in-transit figure.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = StockTransferRepository(session, tenant)
        self.line_repo = StockTransferLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_transfers(
        self, filters: TransferFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[StockTransferRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)
        warehouses = await self.inventory.warehouse_facts(
            [w for row in rows for w in (row.from_warehouse_id, row.to_warehouse_id)]
        )
        return [self._to_read(row, warehouses=warehouses) for row in rows], pagination

    async def get_transfer(self, transfer_id: uuid.UUID) -> StockTransferRead:
        transfer = await self.repo.get_with_lines(transfer_id)
        if transfer is None:
            raise NotFoundError("Stock transfer", transfer_id)
        return await self._detail(transfer)

    # --- writes -------------------------------------------------------------

    async def create(self, payload: StockTransferCreate) -> StockTransferRead:
        async with transaction(self.session):
            warehouses = await self.inventory.warehouse_facts(
                [payload.from_warehouse_id, payload.to_warehouse_id]
            )
            assert_warehouse_scope(self.tenant, payload.from_warehouse_id, payload.to_warehouse_id)
            for warehouse_id in (payload.from_warehouse_id, payload.to_warehouse_id):
                facts = warehouses.get(warehouse_id)
                if facts is None:
                    raise NotFoundError("Warehouse", warehouse_id)
                if not facts.is_active:
                    raise BusinessRuleError(
                        f"Warehouse {facts.code} is inactive and cannot take part in a transfer.",
                        details={"warehouse_code": facts.code},
                    )

            items = await self.inventory.item_facts([line.item_id for line in payload.lines])
            missing = [line.item_id for line in payload.lines if line.item_id not in items]
            if missing:
                raise NotFoundError("Item", missing[0])

            await _adopt_named_serials(self.session, self.tenant, payload.lines, items)
            await self._assert_lines_are_traceable(payload, items)
            await self._validate_line_locations(payload, warehouses)

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.STOCK_TRANSFER
            )
            transfer = StockTransfer(
                code=code,
                status=TransferStatus.DRAFT,
                from_warehouse_id=payload.from_warehouse_id,
                to_warehouse_id=payload.to_warehouse_id,
                transfer_date=payload.transfer_date or today(),
                expected_date=payload.expected_date,
                reason=payload.reason,
                source_type=payload.source_type,
                source_id=payload.source_id,
                notes=payload.notes,
                line_count=len(payload.lines),
                total_quantity=to_quantity(sum(line.quantity for line in payload.lines)),
            )
            self.repo.add(transfer)
            await self.session.flush()

            for number, line in enumerate(payload.lines, start=1):
                self.line_repo.add(
                    StockTransferLine(
                        transfer_id=transfer.id,
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        from_location_id=line.from_location_id,
                        to_location_id=line.to_location_id,
                        quantity=to_quantity(line.quantity),
                        serial_ids=line.serial_ids or None,
                        source_line_id=line.source_line_id,
                        notes=line.notes,
                    )
                )
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_TRANSFER,
                entity_id=transfer.id,
                entity_code=transfer.code,
                summary=f"Created stock transfer {transfer.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    transfer,
                    [
                        "code",
                        "from_warehouse_id",
                        "to_warehouse_id",
                        "transfer_date",
                        "reason",
                        "source_type",
                        "source_id",
                        "line_count",
                        "total_quantity",
                    ],
                ),
            )

        return await self.get_transfer(transfer.id)

    async def update(
        self, transfer_id: uuid.UUID, payload: StockTransferUpdate
    ) -> StockTransferRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            transfer = await self._locked(transfer_id)
            if transfer.status is not TransferStatus.DRAFT:
                raise BusinessRuleError(
                    "Only a draft transfer can be edited.",
                    details={"status": str(transfer.status)},
                )
            before = snapshot(transfer, ["reason", "expected_date", "notes"])
            for field, value in changes.items():
                setattr(transfer, field, value)
            self.repo.touch(transfer)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_TRANSFER,
                entity_id=transfer.id,
                entity_code=transfer.code,
                summary=f"Updated stock transfer {transfer.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(transfer, ["reason", "expected_date", "notes"]),
            )
        return await self.get_transfer(transfer_id)

    async def approve(self, transfer_id: uuid.UUID) -> StockTransferRead:
        async with transaction(self.session):
            transfer = await self._locked(transfer_id)
            TRANSFER_FLOW.assert_transition(transfer.status, TransferStatus.APPROVED)
            previous = transfer.status
            transfer.status = TransferStatus.APPROVED
            transfer.approved_by_id = self.tenant.user_id
            transfer.approved_at = utcnow()
            self.repo.touch(transfer)
            await self.session.flush()
            await self._record_transition(transfer, previous, TransferStatus.APPROVED, None)
        return await self.get_transfer(transfer_id)

    async def dispatch(
        self, transfer_id: uuid.UUID, reason: str | None = None
    ) -> StockTransferRead:
        """Post the outbound leg and put the quantity in transit at the destination.

        This is the decision point for the destination, and the last moment at
        which refusing costs nothing: the document stays APPROVED, which is
        cancellable, and no stock has moved. The destination grains go through
        :meth:`MovementEngine._validate_grain` with ``receiving=True``, which
        applies the same rules :meth:`receive` will apply to the same position -
        deliberately the same code, so the two can never drift apart and leave a
        document that dispatched but cannot be received.

        Checking again here rather than trusting ``create`` is the point: master
        data changes between the two. An item deactivated after the transfer was
        raised is refused here, where the stock is still on the source's shelf.
        """
        async with transaction(self.session):
            # Header lock first, then the lines. Every balance lock this method
            # takes happens after it, so the acquisition order is the same for
            # dispatch, receive and cancel.
            await self._locked(transfer_id)
            transfer = await self.repo.get_with_lines(transfer_id)
            if transfer is None:  # pragma: no cover - the lock already found it
                raise NotFoundError("Stock transfer", transfer_id)
            TRANSFER_FLOW.assert_transition(transfer.status, TransferStatus.IN_TRANSIT)
            assert_warehouse_scope(
                self.tenant, transfer.from_warehouse_id, transfer.to_warehouse_id
            )
            previous = transfer.status

            movements: list[MovementRequest] = []
            movement_slices: list[tuple[int, int]] = []
            for line in transfer.lines:
                start = len(movements)
                serial_ids = line.serial_ids or []
                if serial_ids:
                    # One movement per unit. The unit's own status supplies the
                    # serial lifecycle, and each carries its own cost layer so
                    # the destination can price every serial separately.
                    for serial_id in serial_ids:
                        movements.append(
                            MovementRequest(
                                item_id=line.item_id,
                                warehouse_id=transfer.from_warehouse_id,
                                location_id=line.from_location_id,
                                batch_id=line.batch_id,
                                serial_id=serial_id,
                                quantity=Decimal("1"),
                                transaction_type=TransactionType.TRANSFER_OUT,
                                reference_type=AUDIT_ENTITY_TRANSFER,
                                reference_id=transfer.id,
                                reference_code=transfer.code,
                                reason=reason or transfer.reason,
                            )
                        )
                else:
                    movements.append(
                        MovementRequest(
                            item_id=line.item_id,
                            warehouse_id=transfer.from_warehouse_id,
                            location_id=line.from_location_id,
                            batch_id=line.batch_id,
                            quantity=line.quantity,
                            transaction_type=TransactionType.TRANSFER_OUT,
                            reference_type=AUDIT_ENTITY_TRANSFER,
                            reference_id=transfer.id,
                            reference_code=transfer.code,
                            reason=reason or transfer.reason,
                        )
                    )
                movement_slices.append((start, len(movements)))
            # Destination positions are locked in the same operation as the
            # source ones: two transfers running in opposite directions between
            # the same pair of warehouses would otherwise be able to deadlock.
            destination_grains = [
                BalanceGrain(
                    item_id=line.item_id,
                    warehouse_id=transfer.to_warehouse_id,
                    location_id=line.to_location_id,
                    batch_id=line.batch_id,
                )
                for line in transfer.lines
            ]

            posted, locked = await self.engine.post_with_balances(
                movements,
                extra_grains=destination_grains,
                auto_consume_batches=False,
            )

            for line, grain, (start, end) in zip(
                transfer.lines, destination_grains, movement_slices, strict=True
            ):
                balance = locked[grain]
                balance.in_transit_quantity = to_quantity(
                    Decimal(balance.in_transit_quantity) + Decimal(line.quantity)
                )
                balance.updated_by_id = self.tenant.user_id
                line.dispatched_quantity = line.quantity
                line_posted = posted[start:end]
                # The value the stock left the source with, blended, for display
                # on the document.
                total_quantity = sum((Decimal(txn.quantity) for txn in line_posted), Decimal("0"))
                if total_quantity > 0:
                    line.unit_cost = to_quantity(
                        sum(
                            (Decimal(txn.quantity) * Decimal(txn.unit_cost) for txn in line_posted),
                            Decimal("0"),
                        )
                        / total_quantity
                    )
                elif line_posted:
                    line.unit_cost = Decimal(line_posted[0].unit_cost)
                # And every movement that carried it, so ``receive`` can read
                # the *unblended* per-layer breakdown out of
                # ``inventory_cost_consumptions`` per serial and re-open exactly
                # those costs at the destination.
                line.dispatch_transaction_id = line_posted[0].id if line_posted else None
                line.dispatch_transaction_ids = (
                    [txn.id for txn in line_posted] if line.serial_ids else None
                )

            transfer.status = TransferStatus.IN_TRANSIT
            transfer.dispatched_at = utcnow()
            self.repo.touch(transfer)
            await self.session.flush()
            # Now that the destination's in-transit quantity is written, a lot
            # that has no company-owned units anywhere can be marked CONSUMED.
            await self.engine.consume_empty_batches(movements)

            await self._record_transition(transfer, previous, TransferStatus.IN_TRANSIT, reason)
        return await self.get_transfer(transfer_id)

    async def receive(self, transfer_id: uuid.UUID, reason: str | None = None) -> StockTransferRead:
        """Post the inbound leg and clear the in-transit quantity.

        The inbound movements carry ``completes_in_transit``: these goods left
        their source when this document was dispatched, and the destination was
        validated then with exactly the rules an inbound movement applies. What
        must hold from that moment is that this method accepts whatever
        ``dispatch`` let through - master data changing while a lorry is on the
        road must not be able to refuse the arrival, because there is no edge
        out of IN_TRANSIT except RECEIVED and the stock would be stranded. See
        the in-transit note above :func:`validate_movement`.
        """
        async with transaction(self.session):
            await self._locked(transfer_id)
            transfer = await self.repo.get_with_lines(transfer_id)
            if transfer is None:  # pragma: no cover - the lock already found it
                raise NotFoundError("Stock transfer", transfer_id)
            TRANSFER_FLOW.assert_transition(transfer.status, TransferStatus.RECEIVED)
            assert_warehouse_scope(
                self.tenant, transfer.from_warehouse_id, transfer.to_warehouse_id
            )
            previous = transfer.status

            # One query for the whole document, not one per line: the cost
            # layers each outbound leg consumed, keyed by the movement that
            # consumed them. The arrival re-opens exactly those, so the
            # company's stock is worth the same after the transfer as before it.
            # A line dispatched before FIFO valuation landed has no breakdown to
            # replay and falls back to its stored blended ``unit_cost``.
            dispatch_ids = [
                transaction_id
                for line in transfer.lines
                for transaction_id in (
                    line.dispatch_transaction_ids
                    or (
                        [line.dispatch_transaction_id]
                        if line.dispatch_transaction_id is not None
                        else []
                    )
                )
            ]
            carried = await CostConsumptionRepository(self.session, self.tenant).by_transaction(
                dispatch_ids
            )
            outbound = await LedgerRepository(self.session, self.tenant).by_ids(dispatch_ids)

            movements: list[MovementRequest] = []
            for line in transfer.lines:
                if line.dispatched_quantity <= 0:
                    continue
                serial_ids = line.serial_ids or []
                ids = line.dispatch_transaction_ids or (
                    [line.dispatch_transaction_id]
                    if line.dispatch_transaction_id is not None
                    else []
                )
                if serial_ids:
                    for index, serial_id in enumerate(serial_ids):
                        transaction_id = ids[index] if index < len(ids) else None
                        transaction_row = outbound.get(transaction_id) if transaction_id else None
                        movements.append(
                            MovementRequest(
                                item_id=line.item_id,
                                warehouse_id=transfer.to_warehouse_id,
                                location_id=line.to_location_id,
                                batch_id=line.batch_id,
                                serial_id=serial_id,
                                quantity=Decimal("1"),
                                transaction_type=TransactionType.TRANSFER_IN,
                                reference_type=AUDIT_ENTITY_TRANSFER,
                                reference_id=transfer.id,
                                reference_code=transfer.code,
                                reason=reason or transfer.reason,
                                unit_cost=(
                                    Decimal(transaction_row.unit_cost)
                                    if transaction_row is not None
                                    else Decimal(line.unit_cost)
                                ),
                                cost_layers=carried_cost_layers(carried, transaction_id),
                                completes_in_transit=True,
                            )
                        )
                else:
                    movements.append(
                        MovementRequest(
                            item_id=line.item_id,
                            warehouse_id=transfer.to_warehouse_id,
                            location_id=line.to_location_id,
                            batch_id=line.batch_id,
                            quantity=line.dispatched_quantity,
                            transaction_type=TransactionType.TRANSFER_IN,
                            reference_type=AUDIT_ENTITY_TRANSFER,
                            reference_id=transfer.id,
                            reference_code=transfer.code,
                            reason=reason or transfer.reason,
                            unit_cost=Decimal(line.unit_cost),
                            cost_layers=carried_cost_layers(carried, line.dispatch_transaction_id),
                            completes_in_transit=True,
                        )
                    )
            _, locked = await self.engine.post_with_balances(movements)

            for line in transfer.lines:
                if line.dispatched_quantity <= 0:
                    continue
                grain = BalanceGrain(
                    item_id=line.item_id,
                    warehouse_id=transfer.to_warehouse_id,
                    location_id=line.to_location_id,
                    batch_id=line.batch_id,
                )
                balance = locked[grain]
                remaining = Decimal(balance.in_transit_quantity) - Decimal(line.dispatched_quantity)
                # Clamped at zero: an in-transit figure can only be short if the
                # balance was corrected while the goods were travelling, and the
                # arrival is the fact on the ground.
                balance.in_transit_quantity = to_quantity(max(Decimal("0"), remaining))
                balance.updated_by_id = self.tenant.user_id
                line.received_quantity = line.dispatched_quantity

            transfer.status = TransferStatus.RECEIVED
            transfer.received_at = utcnow()
            self.repo.touch(transfer)
            await self.session.flush()

            await self._record_transition(transfer, previous, TransferStatus.RECEIVED, reason)
        return await self.get_transfer(transfer_id)

    async def cancel(self, transfer_id: uuid.UUID, reason: str) -> StockTransferRead:
        async with transaction(self.session):
            transfer = await self._locked(transfer_id)
            TRANSFER_FLOW.assert_transition(transfer.status, TransferStatus.CANCELLED)
            previous = transfer.status
            transfer.status = TransferStatus.CANCELLED
            self.repo.touch(transfer)
            await self.session.flush()
            await self._record_transition(transfer, previous, TransferStatus.CANCELLED, reason)
        return await self.get_transfer(transfer_id)

    # --- helpers ------------------------------------------------------------

    async def _assert_lines_are_traceable(
        self, payload: StockTransferCreate, items: dict[uuid.UUID, ItemFacts]
    ) -> None:
        """Traceability, decided where the line is keyed rather than at dispatch.

        The movement engine gates batch and serial requirements on movements
        that *start* stock into a position, which the outbound leg of a transfer
        is not. That is right for the engine and leaves this document with a
        hole of its own: a tracked item keyed without its lot or units would get
        as far as ``dispatch`` and come back as "only 0 available", naming
        neither the real problem nor the line it is on. The document answers it
        at creation, where nothing has moved and the operator is looking at the
        line.
        """
        all_serial_ids = [serial_id for line in payload.lines for serial_id in line.serial_ids]
        serials = await StockSerialRepository(self.session, self.tenant).by_ids(all_serial_ids)
        missing = [serial_id for serial_id in all_serial_ids if serial_id not in serials]
        if missing:
            raise NotFoundError("Stock serial", missing[0])

        for number, line in enumerate(payload.lines, start=1):
            item = items[line.item_id]
            if item.is_batch_tracked and line.batch_id is None:
                # A serial carries its lot, so a line naming units has already
                # answered the batch question. Adopt it onto the line so both
                # legs key the same grain.
                if line.serial_ids:
                    batches = {serials[serial_id].batch_id for serial_id in line.serial_ids}
                    if len(batches) != 1 or None in batches:
                        raise BusinessRuleError(
                            f"Line {number}: the serials named belong to different batches or "
                            "none; a batch is required.",
                            code=ErrorCode.BATCH_REQUIRED,
                            details={"line_number": number, "item_code": item.code},
                        )
                    line.batch_id = next(iter(batches))
                else:
                    raise BusinessRuleError(
                        f"Line {number}: {item.code} is batch tracked, so the batch being "
                        "transferred must be named.",
                        code=ErrorCode.BATCH_REQUIRED,
                        details={"line_number": number, "item_code": item.code},
                    )
            if item.is_serial_tracked and not line.serial_ids:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is serial tracked, so the units being "
                    "transferred must be named.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={"line_number": number, "item_code": item.code},
                )
            if not item.is_serial_tracked and line.serial_ids:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is not serial tracked; remove the serials.",
                    details={"line_number": number, "item_code": item.code},
                )
            for serial_id in line.serial_ids:
                serial = serials[serial_id]
                if serial.item_id != line.item_id:
                    raise BusinessRuleError(
                        f"Line {number}: serial {serial.serial_number} belongs to another item.",
                        details={
                            "line_number": number,
                            "serial_number": serial.serial_number,
                            "item_code": item.code,
                        },
                    )
                if line.batch_id is not None and serial.batch_id != line.batch_id:
                    raise BusinessRuleError(
                        f"Line {number}: serial {serial.serial_number} is not in the named batch.",
                        details={
                            "line_number": number,
                            "serial_number": serial.serial_number,
                            "batch_id": str(line.batch_id),
                        },
                    )

    async def _validate_line_locations(
        self, payload: StockTransferCreate, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> None:
        """Both bins on every line, in one query, at the moment they are keyed.

        ``from_location_id`` and ``to_location_id`` arrive as raw request-body
        input and carry company-blind foreign keys, so nothing but this stops a
        line naming another tenant's bin, or a bin in a third warehouse. Waiting
        for the dispatch to notice is far too late in the destination's case:
        the outbound leg posts, the in-transit quantity lands in a position no
        warehouse view will ever show, and the inbound leg - which validates the
        same bin as a movement - then refuses it. TRANSFER_FLOW has no edge out
        of IN_TRANSIT except RECEIVED, so the document, and the stock, would be
        stranded there for good.
        """
        wanted = [
            location_id
            for line in payload.lines
            for location_id in (line.from_location_id, line.to_location_id)
            if location_id is not None
        ]
        if not wanted:
            return
        locations = await self.inventory.location_facts(wanted)

        for number, line in enumerate(payload.lines, start=1):
            for location_id, warehouse_id, receiving in (
                (line.from_location_id, payload.from_warehouse_id, False),
                (line.to_location_id, payload.to_warehouse_id, True),
            ):
                if location_id is None:
                    continue
                location = locations.get(location_id)
                # Another company's, soft-deleted, or never there at all.
                if location is None:
                    raise NotFoundError("Storage location", location_id)
                warehouse = warehouses[warehouse_id]
                if location.warehouse_id != warehouse_id:
                    raise BusinessRuleError(
                        f"Line {number}: storage location {location.code} is not in "
                        f"warehouse {warehouse.code}.",
                        details={
                            "line_number": number,
                            "location_id": str(location_id),
                            "location_code": location.code,
                            "warehouse_code": warehouse.code,
                        },
                    )
                # The same asymmetry every other bin rule follows: a retired bin
                # may give its stock up but may not take any in, so only the
                # destination is held to the flag.
                if receiving and not location.is_active:
                    raise BusinessRuleError(
                        f"Line {number}: storage location {location.code} is inactive and "
                        "cannot receive stock.",
                        details={
                            "line_number": number,
                            "location_id": str(location_id),
                            "location_code": location.code,
                        },
                    )

    async def _locked(self, transfer_id: uuid.UUID) -> StockTransfer:
        """Serialisation point for every state change on this document.

        Without it two simultaneous requests both read the same status, both
        pass ``assert_transition`` and both act: a double-clicked dispatch posts
        TRANSFER_OUT twice and removes twice the stock the document orders.
        """
        transfer: StockTransfer = await _lock_document(
            self.session, self.repo.lock_for_update, "Stock transfer", transfer_id
        )
        return transfer

    async def _record_transition(
        self,
        transfer: StockTransfer,
        previous: TransferStatus,
        target: TransferStatus,
        reason: str | None,
    ) -> None:
        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ENTITY_TRANSFER,
            entity_id=transfer.id,
            entity_code=transfer.code,
            from_status=str(previous),
            to_status=str(target),
            user_id=self.tenant.user_id,
            reason=reason,
        )

    async def _detail(self, transfer: StockTransfer) -> StockTransferRead:
        warehouses = await self.inventory.warehouse_facts(
            [transfer.from_warehouse_id, transfer.to_warehouse_id]
        )
        items = await self.inventory.item_facts([line.item_id for line in transfer.lines])
        read = self._to_read(transfer, warehouses=warehouses)
        read.lines = [self._line_read(line, items.get(line.item_id)) for line in transfer.lines]
        return read

    @staticmethod
    def _line_read(line: StockTransferLine, item: ItemFacts | None) -> StockTransferLineRead:
        read = StockTransferLineRead.model_validate(line)
        read.item_code = item.code if item else None
        read.item_name = item.name if item else None
        return read

    @staticmethod
    def _to_read(
        transfer: StockTransfer, *, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> StockTransferRead:
        _declare_lines_empty_if_unloaded(transfer)
        read = StockTransferRead.model_validate(transfer)
        source = warehouses.get(transfer.from_warehouse_id)
        destination = warehouses.get(transfer.to_warehouse_id)
        read.from_warehouse_code = source.code if source else None
        read.to_warehouse_code = destination.code if destination else None
        return read


# -----------------------------------------------------------------------------
# Stock adjustment service
# -----------------------------------------------------------------------------


#: The adjustment header fields an audit row compares, on create and on edit.
_ADJUSTMENT_AUDITED_FIELDS = [
    "code",
    "warehouse_id",
    "adjustment_date",
    "cost_centre_id",
    "reason_code",
    "reason",
    "total_value",
    "requires_approval",
    "line_count",
]


class StockAdjustmentService:
    """Write-ups and write-downs, with the approval control section 42 requires."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = StockAdjustmentRepository(session, tenant)
        self.line_repo = StockAdjustmentLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.layers = CostLayerRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_adjustments(
        self, filters: AdjustmentFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[StockAdjustmentRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        return [self._to_read(row, warehouses) for row in rows], pagination

    async def get_adjustment(self, adjustment_id: uuid.UUID) -> StockAdjustmentRead:
        adjustment = await self.repo.get_with_lines(adjustment_id)
        if adjustment is None:
            raise NotFoundError("Stock adjustment", adjustment_id)
        warehouses = await self.inventory.warehouse_facts([adjustment.warehouse_id])
        items = await self.inventory.item_facts([line.item_id for line in adjustment.lines])
        read = self._to_read(adjustment, warehouses)
        read.lines = []
        for line in adjustment.lines:
            line_read = StockAdjustmentLineRead.model_validate(line)
            item = items.get(line.item_id)
            line_read.item_code = item.code if item else None
            line_read.item_name = item.name if item else None
            read.lines.append(line_read)
        return read

    # --- writes -------------------------------------------------------------

    async def create(
        self, payload: StockAdjustmentCreate, *, can_enter_cost: bool = True
    ) -> StockAdjustmentRead:
        """Draft the document and work out whether it needs an approval.

        Line costs are read from the position's **FIFO cost layers** - the ones
        the posting will really consume - so the document's value, the approval
        decision measured against it and the cost the ledger books are one
        number rather than three.

        They used not to be. The value came off ``average_cost``, which under a
        moving average was the same figure by construction and under FIFO is not:
        a write-off of the oldest, dearest stock showed the blended price and
        posted the layer price. See :class:`FifoCostPreview` for the worked
        example, and :meth:`post` for the re-check that catches a preview which
        has gone stale between the draft and the posting.

        ``can_enter_cost`` is the caller's ``item.cost_view`` or
        ``inventory.valuation_view``: only someone who may see what stock costs
        may state the cost a write-up is booked at. Everyone else's write-up is
        priced by the server's own precedence - see :func:`inbound_unit_cost`.
        """
        self._assert_cost_entry_allowed(payload, can_enter_cost)
        async with transaction(self.session):
            assert_warehouse_scope(self.tenant, payload.warehouse_id)
            items = await self._check_header(payload)
            if payload.cost_centre_id is not None:
                await require_cost_centre(self.session, self.tenant, payload.cost_centre_id)

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.STOCK_ADJUSTMENT
            )
            adjustment = StockAdjustment(
                code=code,
                status=AdjustmentStatus.DRAFT,
                warehouse_id=payload.warehouse_id,
                adjustment_date=payload.adjustment_date or today(),
                cost_centre_id=payload.cost_centre_id,
                reason_code=payload.reason_code,
                reason=payload.reason,
                notes=payload.notes,
                line_count=len(payload.lines),
            )
            self.repo.add(adjustment)
            await self.session.flush()

            await self._write_lines(adjustment, payload, items)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.STOCK_ADJUSTMENT,
                entity_type=AUDIT_ENTITY_ADJUSTMENT,
                entity_id=adjustment.id,
                entity_code=adjustment.code,
                summary=(
                    f"Drafted stock adjustment {adjustment.code} "
                    f"({adjustment.reason_code}, value {adjustment.total_value})"
                ),
                user_id=self.tenant.user_id,
                new_values=snapshot(adjustment, _ADJUSTMENT_AUDITED_FIELDS),
            )

        return await self.get_adjustment(adjustment.id)

    async def update(
        self,
        adjustment_id: uuid.UUID,
        payload: StockAdjustmentUpdate,
        *,
        can_enter_cost: bool = True,
    ) -> StockAdjustmentRead:
        """Replace a draft's header and lines, re-priced exactly as :meth:`create` prices them.

        Only a draft: once submitted the document is what an approver is looking
        at, and once approved it is what they signed. The lines are replaced
        rather than patched one by one - a draft has no history of its own to
        keep, and re-pricing the whole document against the layers as they
        stand now is what keeps its value, and so its approval decision, honest.
        """
        self._assert_cost_entry_allowed(payload, can_enter_cost)
        async with transaction(self.session):
            await self._locked(adjustment_id)
            adjustment = await self.repo.get_with_lines(adjustment_id)
            if adjustment is None:  # pragma: no cover - the lock already found it
                raise NotFoundError("Stock adjustment", adjustment_id)
            if adjustment.status is not AdjustmentStatus.DRAFT:
                raise BusinessRuleError(
                    "Only a draft adjustment can be edited.",
                    details={"status": str(adjustment.status)},
                )
            assert_warehouse_scope(self.tenant, adjustment.warehouse_id, payload.warehouse_id)
            items = await self._check_header(payload)
            # A re-save of the centre the draft already names allocates nothing
            # new, and keeps working after the centre is deactivated.
            if (
                payload.cost_centre_id is not None
                and payload.cost_centre_id != adjustment.cost_centre_id
            ):
                await require_cost_centre(self.session, self.tenant, payload.cost_centre_id)

            before = snapshot(adjustment, _ADJUSTMENT_AUDITED_FIELDS)
            # Old lines out first, and flushed: the replacements reuse their line
            # numbers, and the unique constraint is checked per statement.
            adjustment.lines.clear()
            await self.session.flush()

            adjustment.warehouse_id = payload.warehouse_id
            adjustment.adjustment_date = payload.adjustment_date or adjustment.adjustment_date
            adjustment.cost_centre_id = payload.cost_centre_id
            adjustment.reason_code = payload.reason_code
            adjustment.reason = payload.reason
            adjustment.notes = payload.notes
            adjustment.line_count = len(payload.lines)
            self.repo.touch(adjustment)
            await self._write_lines(adjustment, payload, items)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_ADJUSTMENT,
                entity_id=adjustment.id,
                entity_code=adjustment.code,
                summary=f"Updated draft stock adjustment {adjustment.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(adjustment, _ADJUSTMENT_AUDITED_FIELDS),
            )
        return await self.get_adjustment(adjustment_id)

    @staticmethod
    def _assert_cost_entry_allowed(payload: StockAdjustmentCreate, can_enter_cost: bool) -> None:
        """Refuse a stated unit cost from someone who may not see costs.

        Refused rather than silently dropped: a figure the operator typed and
        the server then ignored would be a document that does not say what its
        author meant.
        """
        if can_enter_cost:
            return
        if any(line.unit_cost is not None for line in payload.lines):
            raise PermissionDeniedError(
                f"{Permission.ITEM_COST_VIEW} or {Permission.INVENTORY_VALUATION_VIEW}"
            )

    async def _check_header(self, payload: StockAdjustmentCreate) -> dict[uuid.UUID, ItemFacts]:
        """The warehouse and every item the draft names exist in this tenant."""
        warehouses = await self.inventory.warehouse_facts([payload.warehouse_id])
        if payload.warehouse_id not in warehouses:
            raise NotFoundError("Warehouse", payload.warehouse_id)
        items = await self.inventory.item_facts([line.item_id for line in payload.lines])
        missing = [line.item_id for line in payload.lines if line.item_id not in items]
        if missing:
            raise NotFoundError("Item", missing[0])

        await _adopt_named_serials(self.session, self.tenant, payload.lines, items)
        serial_ids = [serial_id for line in payload.lines for serial_id in line.serial_ids]
        serials = await StockSerialRepository(self.session, self.tenant).by_ids(serial_ids)
        missing_serials = [serial_id for serial_id in serial_ids if serial_id not in serials]
        if missing_serials:
            raise NotFoundError("Stock serial", missing_serials[0])

        for number, line in enumerate(payload.lines, start=1):
            item = items[line.item_id]
            if item.is_serial_tracked and not line.serial_ids:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is serial tracked, so the units being "
                    "adjusted must be named.",
                    code=ErrorCode.SERIAL_REQUIRED,
                    details={"line_number": number, "item_code": item.code},
                )
            if not item.is_serial_tracked and line.serial_ids:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is not serial tracked; remove the serials.",
                    details={"line_number": number, "item_code": item.code},
                )
            if line.serial_ids:
                batches = {serials[serial_id].batch_id for serial_id in line.serial_ids}
                if item.is_batch_tracked and line.batch_id is None:
                    if len(batches) != 1 or None in batches:
                        raise BusinessRuleError(
                            f"Line {number}: the units named span several batches or none; "
                            "a batch is required.",
                            code=ErrorCode.BATCH_REQUIRED,
                            details={"line_number": number, "item_code": item.code},
                        )
                    line.batch_id = next(iter(batches))
                for serial_id in line.serial_ids:
                    serial = serials[serial_id]
                    if serial.item_id != line.item_id:
                        raise BusinessRuleError(
                            f"Line {number}: serial {serial.serial_number} belongs to another "
                            "item.",
                            details={"line_number": number, "item_code": item.code},
                        )
                    if line.batch_id is not None and serial.batch_id != line.batch_id:
                        raise BusinessRuleError(
                            f"Line {number}: serial {serial.serial_number} is not in the "
                            "named batch.",
                            details={
                                "line_number": number,
                                "serial_number": serial.serial_number,
                                "batch_id": str(line.batch_id),
                            },
                        )
            elif item.is_batch_tracked and line.batch_id is None:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is batch tracked, so the batch being "
                    "adjusted must be named.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"line_number": number, "item_code": item.code},
                )
        return items

    async def _write_lines(
        self,
        adjustment: StockAdjustment,
        payload: StockAdjustmentCreate,
        items: dict[uuid.UUID, ItemFacts],
    ) -> None:
        """Stage the draft's lines, each priced off the layers it will consume.

        The collection is declared loaded with exactly these lines, so a read in
        the same session sees them without a lazy load - ``lines`` is
        ``lazy="raise"``.
        """
        grains = [
            BalanceGrain(
                item_id=line.item_id,
                warehouse_id=payload.warehouse_id,
                location_id=line.location_id,
                batch_id=line.batch_id,
            )
            for line in payload.lines
        ]
        preview = await FifoCostPreview.load(self.inventory, self.layers, grains)

        total_value = Decimal("0")
        lines: list[StockAdjustmentLine] = []
        for number, (line, grain) in enumerate(zip(payload.lines, grains, strict=True), start=1):
            quantity = to_quantity(line.quantity)
            cost = preview.line_cost(
                grain,
                quantity,
                supplied=line.unit_cost,
                fallback_cost=items[line.item_id].standard_cost,
            )
            total_value += cost.total_cost
            lines.append(
                self.line_repo.add(
                    StockAdjustmentLine(
                        adjustment_id=adjustment.id,
                        line_number=number,
                        item_id=line.item_id,
                        location_id=line.location_id,
                        batch_id=line.batch_id,
                        quantity=quantity,
                        serial_ids=line.serial_ids or None,
                        unit_cost=cost.unit_cost,
                        total_cost=cost.total_cost,
                        system_quantity=preview.on_hand(grain),
                        notes=line.notes,
                    )
                )
            )
        set_committed_value(adjustment, "lines", lines)
        self._set_value(adjustment, total_value)

    async def submit(self, adjustment_id: uuid.UUID) -> StockAdjustmentRead:
        """Send the document for approval, re-priced against the layers as they are now.

        The re-pricing is not decoration. A draft is costed off the layers
        standing at the time it was drafted, and stock moves; a document whose
        real cost has risen above the threshold since then would otherwise be
        stuck - :meth:`post` refuses it for needing an approval, and this method
        would refuse to send it for one, because the stale figure says it does
        not need it. Re-pricing here is what makes the refusal in ``post``
        actionable instead of a dead end.
        """
        async with transaction(self.session):
            await self._locked(adjustment_id)
            adjustment = await self.repo.get_with_lines(adjustment_id)
            if adjustment is None:  # pragma: no cover - the lock already found it
                raise NotFoundError("Stock adjustment", adjustment_id)
            ADJUSTMENT_FLOW.assert_transition(adjustment.status, AdjustmentStatus.PENDING_APPROVAL)
            await self._reprice(adjustment)
            if not adjustment.requires_approval:
                raise BusinessRuleError(
                    f"Adjustment {adjustment.code} is below the approval threshold and can "
                    "be posted directly.",
                    details={
                        "total_value": str(adjustment.total_value),
                        "threshold": str(ADJUSTMENT_APPROVAL_VALUE_THRESHOLD),
                    },
                )
            previous = adjustment.status
            adjustment.status = AdjustmentStatus.PENDING_APPROVAL
            self.repo.touch(adjustment)
            await self.session.flush()
            await self._record_transition(
                adjustment, previous, AdjustmentStatus.PENDING_APPROVAL, None
            )
        return await self.get_adjustment(adjustment_id)

    async def approve(self, adjustment_id: uuid.UUID, note: str | None) -> StockAdjustmentRead:
        async with transaction(self.session):
            adjustment = await self._locked(adjustment_id)
            ADJUSTMENT_FLOW.assert_transition(adjustment.status, AdjustmentStatus.APPROVED)
            self._assert_separate_approver(adjustment)

            previous = adjustment.status
            adjustment.status = AdjustmentStatus.APPROVED
            adjustment.approved_by_id = self.tenant.user_id
            adjustment.approved_at = utcnow()
            adjustment.decision_note = note
            self.repo.touch(adjustment)
            await self.session.flush()
            await self._record_transition(
                adjustment, previous, AdjustmentStatus.APPROVED, note, action=AuditAction.APPROVE
            )
        return await self.get_adjustment(adjustment_id)

    async def reject(self, adjustment_id: uuid.UUID, note: str) -> StockAdjustmentRead:
        """Refuse a pending adjustment. The reason is required - ``RejectionRequest``."""
        async with transaction(self.session):
            adjustment = await self._locked(adjustment_id)
            ADJUSTMENT_FLOW.assert_transition(adjustment.status, AdjustmentStatus.REJECTED)
            self._assert_separate_approver(adjustment)

            previous = adjustment.status
            adjustment.status = AdjustmentStatus.REJECTED
            adjustment.decision_note = note
            self.repo.touch(adjustment)
            await self.session.flush()
            await self._record_transition(
                adjustment, previous, AdjustmentStatus.REJECTED, note, action=AuditAction.REJECT
            )
        return await self.get_adjustment(adjustment_id)

    async def post(self, adjustment_id: uuid.UUID) -> StockAdjustmentRead:
        """Turn the document into stock movements.

        A document over the value threshold cannot take the DRAFT -> POSTED edge:
        it has to be approved first, and the approval is recorded against a
        different user than the one who raised it.

        The threshold is checked **twice**, and the second check is the one that
        makes the control real.

        The first is the gate below, measured on ``total_value`` as the draft
        priced it. That figure is now a FIFO preview (:class:`FifoCostPreview`)
        rather than a quantity times an average, so it is the right number at
        the moment it was taken - but a preview reads the layers without a lock
        and stock moves. A receipt at a higher price, another issue eating the
        cheap layers, and the write-off the document will actually charge is no
        longer the one the gate saw.

        So after the movements are posted the document is restated from the
        ledger rows the engine wrote - the real, layer-by-layer cost - and if a
        document that was never approved turns out to have cost more than the
        threshold, the posting is refused and the whole transaction unwinds.
        Nothing reaches the ledger. The alternative, trusting the preview,
        leaves the hole this fix exists to close: the approval gate would be
        measured with one ruler and the write-off booked with another.

        Deliberately only for the DRAFT -> POSTED edge. An APPROVED document
        posts whatever it turns out to cost: a person looked at it and signed
        it, and refusing it here would mean a document could be approved and
        then become unpostable because someone else moved stock. The restated
        value is recorded on the document either way, so what it was approved
        for and what it booked are both on the record.
        """
        async with transaction(self.session):
            # Header lock before the status is read: two simultaneous posts
            # would otherwise both see APPROVED and both write the document to
            # the ledger, doubling the write-off.
            await self._locked(adjustment_id)
            adjustment = await self.repo.get_with_lines(adjustment_id)
            if adjustment is None:  # pragma: no cover - the lock already found it
                raise NotFoundError("Stock adjustment", adjustment_id)
            ADJUSTMENT_FLOW.assert_transition(adjustment.status, AdjustmentStatus.POSTED)
            assert_warehouse_scope(self.tenant, adjustment.warehouse_id)

            if adjustment.status is AdjustmentStatus.DRAFT and adjustment.requires_approval:
                raise BusinessRuleError(
                    f"Adjustment {adjustment.code} is worth {adjustment.total_value} and "
                    f"must be approved before it can post.",
                    code=ErrorCode.APPROVAL_REQUIRED,
                    status_code=422,
                    details={
                        "total_value": str(adjustment.total_value),
                        "threshold": str(ADJUSTMENT_APPROVAL_VALUE_THRESHOLD),
                    },
                )

            previous = adjustment.status
            movements: list[MovementRequest] = []
            movement_slices: list[tuple[int, int]] = []
            for line in adjustment.lines:
                start = len(movements)
                transaction_type = (
                    TransactionType.ADJUSTMENT_INCREASE
                    if line.quantity > 0
                    else TransactionType.ADJUSTMENT_DECREASE
                )
                common: dict[str, Any] = {
                    "item_id": line.item_id,
                    "warehouse_id": adjustment.warehouse_id,
                    "location_id": line.location_id,
                    "batch_id": line.batch_id,
                    "transaction_type": transaction_type,
                    "reference_type": AUDIT_ENTITY_ADJUSTMENT,
                    "reference_id": adjustment.id,
                    "reference_code": adjustment.code,
                    "reason": f"{adjustment.reason_code}: {adjustment.reason}"[:255],
                    "unit_cost": Decimal(line.unit_cost),
                    "occurred_at": datetime.combine(adjustment.adjustment_date, utcnow().timetz()),
                }
                if line.serial_ids:
                    for serial_id in line.serial_ids:
                        movements.append(
                            MovementRequest(**common, serial_id=serial_id, quantity=Decimal("1"))
                        )
                else:
                    movements.append(
                        MovementRequest(**common, quantity=abs(Decimal(line.quantity)))
                    )
                movement_slices.append((start, len(movements)))
            transactions = await self.engine.post(movements)
            # What the ledger charged, layer by layer - not what the draft
            # estimated. From here on the document says the same number the
            # stock ledger does.
            self._restate_from_ledger(
                adjustment,
                [transactions[start:end] for start, end in movement_slices],
            )
            if (
                previous is AdjustmentStatus.DRAFT
                and adjustment.total_value > ADJUSTMENT_APPROVAL_VALUE_THRESHOLD
            ):
                raise BusinessRuleError(
                    f"Adjustment {adjustment.code} costs {adjustment.total_value} against the "
                    "cost layers it consumes and must be approved before it can post.",
                    code=ErrorCode.APPROVAL_REQUIRED,
                    status_code=422,
                    details={
                        "total_value": str(adjustment.total_value),
                        "threshold": str(ADJUSTMENT_APPROVAL_VALUE_THRESHOLD),
                    },
                )

            adjustment.status = AdjustmentStatus.POSTED
            adjustment.posted_at = utcnow()
            self.repo.touch(adjustment)
            await self.session.flush()

            # Section 83: an adjustment is always audited as a STOCK_ADJUSTMENT,
            # on top of the per-movement INVENTORY_MOVEMENT rows the engine
            # writes, so the write-off report can be built from the audit trail
            # without joining the ledger.
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.STOCK_ADJUSTMENT,
                entity_type=AUDIT_ENTITY_ADJUSTMENT,
                entity_id=adjustment.id,
                entity_code=adjustment.code,
                summary=(
                    f"Posted stock adjustment {adjustment.code} "
                    f"({adjustment.reason_code}, value {adjustment.total_value})"
                ),
                user_id=self.tenant.user_id,
                old_values={"status": str(previous)},
                new_values={"status": str(AdjustmentStatus.POSTED)},
                context={
                    "reason_code": str(adjustment.reason_code),
                    "reason": adjustment.reason,
                    "total_value": adjustment.total_value,
                    "line_count": adjustment.line_count,
                    "approved_by_id": adjustment.approved_by_id,
                },
            )
        return await self.get_adjustment(adjustment_id)

    async def cancel(self, adjustment_id: uuid.UUID, reason: str) -> StockAdjustmentRead:
        async with transaction(self.session):
            adjustment = await self._locked(adjustment_id)
            ADJUSTMENT_FLOW.assert_transition(adjustment.status, AdjustmentStatus.CANCELLED)
            previous = adjustment.status
            adjustment.status = AdjustmentStatus.CANCELLED
            adjustment.decision_note = reason
            self.repo.touch(adjustment)
            await self.session.flush()
            await self._record_transition(
                adjustment,
                previous,
                AdjustmentStatus.CANCELLED,
                reason,
                action=AuditAction.CANCEL,
            )
        return await self.get_adjustment(adjustment_id)

    # --- helpers ------------------------------------------------------------

    async def _locked(self, adjustment_id: uuid.UUID) -> StockAdjustment:
        """Serialisation point for every state change on this document.

        The posting path is the one that matters: without the lock, two requests
        both read APPROVED, both pass ``assert_transition`` and both post every
        line to the ledger. The balance row locks make the two postings
        sequential; they do not make the second one illegal.
        """
        adjustment: StockAdjustment = await _lock_document(
            self.session, self.repo.lock_for_update, "Stock adjustment", adjustment_id
        )
        return adjustment

    def _assert_separate_approver(self, adjustment: StockAdjustment) -> None:
        # ASSUMPTION: the approver may not be the person who raised the document,
        # unless they are a platform administrator. This applies the project-wide
        # segregation-of-duties decision already recorded in docs/assumptions.md
        # section 2 to stock adjustments.
        if self.tenant.is_superuser:
            return
        if adjustment.created_by_id == self.tenant.user_id:
            raise BusinessRuleError(
                "An adjustment must be approved by someone other than the person who raised it.",
                details={"adjustment_code": adjustment.code},
            )

    @staticmethod
    def _set_value(adjustment: StockAdjustment, total: Decimal) -> None:
        """The document's value and its approval flag, always set together.

        One place, because they are one decision: a value written without the
        flag beside it is how a document comes to state one number and be gated
        on another.
        """
        adjustment.total_value = to_money(total)
        adjustment.requires_approval = adjustment.total_value > ADJUSTMENT_APPROVAL_VALUE_THRESHOLD

    async def _reprice(self, adjustment: StockAdjustment) -> Decimal:
        """Re-cost every line against the position's layers as they stand now.

        The same preview the draft was priced with, run again. Lines are costed
        in line-number order so two lines against one position consume the queue
        in the order the posting will.
        """
        lines = sorted(adjustment.lines, key=lambda line: line.line_number)
        grains = [
            BalanceGrain(
                item_id=line.item_id,
                warehouse_id=adjustment.warehouse_id,
                location_id=line.location_id,
                batch_id=line.batch_id,
            )
            for line in lines
        ]
        items = await self.inventory.item_facts([line.item_id for line in lines])
        preview = await FifoCostPreview.load(self.inventory, self.layers, grains)

        total = Decimal("0")
        for line, grain in zip(lines, grains, strict=True):
            item = items.get(line.item_id)
            cost = preview.line_cost(
                grain,
                Decimal(line.quantity),
                supplied=Decimal(line.unit_cost),
                fallback_cost=item.standard_cost if item else Decimal("0"),
            )
            line.unit_cost = cost.unit_cost
            line.total_cost = cost.total_cost
            self.line_repo.touch(line)
            total += cost.total_cost
        self._set_value(adjustment, total)
        self.repo.touch(adjustment)
        return adjustment.total_value

    def _restate_from_ledger(
        self,
        adjustment: StockAdjustment,
        transactions: Sequence[Sequence[InventoryTransaction]],
    ) -> Decimal:
        """Write what the engine actually charged back onto the document.

        One inner sequence per line. A serialised line posts one movement per
        unit, and each of those can draw from a different layer, so the line is
        restated from its own slice rather than from a single blended row.

        ``total_cost`` on a ledger row is a magnitude - the sign lives on
        ``quantity`` - and the header adds the magnitudes up, exactly as the
        draft did, so a document that writes 10 up and 10 down is worth what it
        moved rather than nothing.
        """
        total = Decimal("0")
        for line, rows in zip(adjustment.lines, transactions, strict=True):
            quantity = sum((abs(Decimal(row.quantity)) for row in rows), Decimal("0"))
            if quantity > 0:
                line.unit_cost = to_unit_price(
                    sum(
                        (abs(Decimal(row.quantity)) * Decimal(row.unit_cost) for row in rows),
                        Decimal("0"),
                    )
                    / quantity
                )
            elif rows:
                line.unit_cost = to_unit_price(rows[0].unit_cost)
            line.total_cost = to_money(
                sum((abs(Decimal(row.total_cost)) for row in rows), Decimal("0"))
            )
            self.line_repo.touch(line)
            total += line.total_cost
        self._set_value(adjustment, total)
        return adjustment.total_value

    async def _record_transition(
        self,
        adjustment: StockAdjustment,
        previous: AdjustmentStatus,
        target: AdjustmentStatus,
        reason: str | None,
        *,
        action: AuditAction = AuditAction.STATUS_CHANGE,
    ) -> None:
        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ENTITY_ADJUSTMENT,
            entity_id=adjustment.id,
            entity_code=adjustment.code,
            from_status=str(previous),
            to_status=str(target),
            user_id=self.tenant.user_id,
            action=action,
            reason=reason,
        )

    @staticmethod
    def _to_read(
        adjustment: StockAdjustment, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> StockAdjustmentRead:
        _declare_lines_empty_if_unloaded(adjustment)
        read = StockAdjustmentRead.model_validate(adjustment)
        warehouse = warehouses.get(adjustment.warehouse_id)
        read.warehouse_code = warehouse.code if warehouse else None
        return read


# -----------------------------------------------------------------------------
# Physical count service
# -----------------------------------------------------------------------------


class PhysicalCountService:
    """Stock takes: snapshot, count, confirm, post the variance."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = PhysicalCountRepository(session, tenant)
        self.line_repo = PhysicalCountLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.layers = CostLayerRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_counts(
        self, filters: PhysicalCountFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[PhysicalCountRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        return [self._to_read(row, warehouses) for row in rows], pagination

    async def get_count(self, count_id: uuid.UUID) -> PhysicalCountRead:
        count = await self.repo.get_or_404(count_id)
        warehouses = await self.inventory.warehouse_facts([count.warehouse_id])
        return self._to_read(count, warehouses)

    async def list_lines(
        self, count_id: uuid.UUID, params: PageParams
    ) -> tuple[list[PhysicalCountLineRead], OffsetPagination]:
        """Lines are paginated separately - a warehouse sheet is thousands long."""
        await self.repo.get_or_404(count_id)
        stmt = self.line_repo.build_list_stmt(count_id)
        rows, pagination = await paginate(self.session, stmt, params)
        items = await self.inventory.item_facts([row.item_id for row in rows])
        results: list[PhysicalCountLineRead] = []
        for row in rows:
            read = PhysicalCountLineRead.model_validate(row)
            item = items.get(row.item_id)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            results.append(read)
        return results, pagination

    # --- writes -------------------------------------------------------------

    async def create(self, payload: PhysicalCountCreate) -> PhysicalCountRead:
        """Build the sheet, snapshotting the system quantity of every position."""
        async with transaction(self.session):
            assert_warehouse_scope(self.tenant, payload.warehouse_id)
            warehouses = await self.inventory.warehouse_facts([payload.warehouse_id])
            warehouse = warehouses.get(payload.warehouse_id)
            if warehouse is None:
                raise NotFoundError("Warehouse", payload.warehouse_id)

            # "Is a sheet already open?" is a check-then-act: two simultaneous
            # requests both read "no" and both create one, which is exactly the
            # situation open_count_exists exists to prevent - each sheet then
            # snapshots the other's variance postings. There is no unique index
            # that can express "at most one non-terminal sheet per warehouse",
            # so the two callers are serialised on an advisory lock held to the
            # end of the transaction that creates the sheet.
            await advisory_xact_lock(
                self.session, "physical_count", self.tenant.company_id, payload.warehouse_id
            )
            if await self.repo.open_count_exists(payload.warehouse_id):
                raise BusinessRuleError(
                    f"A stock count is already open for warehouse {warehouse.code}. "
                    "Finish or cancel it before starting another.",
                    details={"warehouse_code": warehouse.code},
                )

            positions = await self.inventory.positions_for_count(
                warehouse_id=payload.warehouse_id,
                location_id=payload.location_id,
                item_ids=payload.item_ids,
                include_zero=payload.include_zero_positions,
                limit=MAX_COUNT_LINES + 1,
            )
            if len(positions) > MAX_COUNT_LINES:
                raise BusinessRuleError(
                    f"That scope covers more than {MAX_COUNT_LINES} positions. Narrow it to "
                    "a location or a set of items.",
                    details={"limit": MAX_COUNT_LINES},
                )
            if not positions:
                raise BusinessRuleError(
                    "There is nothing to count in that scope.",
                    details={"warehouse_code": warehouse.code},
                )

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.PHYSICAL_COUNT
            )
            count = PhysicalCount(
                code=code,
                status=PhysicalCountStatus.DRAFT,
                warehouse_id=payload.warehouse_id,
                location_id=payload.location_id,
                count_date=payload.count_date or today(),
                reason=payload.reason,
                notes=payload.notes,
                line_count=len(positions),
            )
            self.repo.add(count)
            await self.session.flush()

            for number, position in enumerate(positions, start=1):
                # ``unit_cost`` is the position's derived cost basis at the
                # moment the sheet was cut - ``stock_value / on_hand`` - and it
                # is exactly two things: what the sheet shows as "cost per unit",
                # and the price stock the count *finds* is re-booked at, which
                # is what the engine does with an arrival's supplied cost.
                #
                # It is deliberately **not** what a shortage is worth. Under
                # FIFO the units that are missing are the oldest ones, and they
                # cost whatever their layers cost, which is not the blended
                # figure - see :meth:`enter_counts` and :meth:`confirm`.
                self.line_repo.add(
                    PhysicalCountLine(
                        count_id=count.id,
                        line_number=number,
                        item_id=position.item_id,
                        location_id=position.location_id,
                        batch_id=position.batch_id,
                        system_quantity=Decimal(position.on_hand_quantity),
                        unit_cost=Decimal(position.average_cost),
                    )
                )
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_COUNT,
                entity_id=count.id,
                entity_code=count.code,
                summary=f"Opened stock count {count.code} for {warehouse.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    count, ["code", "warehouse_id", "location_id", "count_date", "line_count"]
                ),
            )
        return await self.get_count(count.id)

    async def start(self, count_id: uuid.UUID) -> PhysicalCountRead:
        async with transaction(self.session):
            count = await self._locked(count_id)
            COUNT_FLOW.assert_transition(count.status, PhysicalCountStatus.IN_PROGRESS)
            previous = count.status
            count.status = PhysicalCountStatus.IN_PROGRESS
            count.started_at = count.started_at or utcnow()
            self.repo.touch(count)
            await self.session.flush()
            await self._record_transition(count, previous, PhysicalCountStatus.IN_PROGRESS, None)
        return await self.get_count(count_id)

    async def enter_counts(
        self, count_id: uuid.UUID, payload: PhysicalCountEntryRequest
    ) -> PhysicalCountRead:
        """Record counted quantities against the sheet's lines.

        The value of a variance is previewed against the position's FIFO cost
        layers, not worked out as ``variance_quantity x unit_cost``. A shortage
        takes the oldest stock, which is not priced at the position's blended
        cost the moment the position holds more than one layer: on 100 @ 500
        followed by 900 @ 10, a count 60 short is a 30 000 shrinkage and the
        blended arithmetic reported 3 540.

        It is still an estimate - the sheet has posted nothing yet, and the
        engine locks nothing here - which is why :meth:`confirm` overwrites it
        with what the ledger actually booked.
        """
        async with transaction(self.session):
            # The header lock makes "are all lines counted now?" a question with
            # one answer: without it two simultaneous entry requests each count
            # the other's lines as still outstanding, and a sheet whose last two
            # lines are entered at the same moment never reaches COUNTED.
            count = await self._locked(count_id)
            if count.status is not PhysicalCountStatus.IN_PROGRESS:
                raise BusinessRuleError(
                    "Counts can only be entered while the sheet is in progress.",
                    details={"status": str(count.status)},
                )

            lines = await self.line_repo.by_ids(
                count_id, [entry.line_id for entry in payload.entries]
            )
            by_id = {line.id: line for line in lines}
            unknown = [e.line_id for e in payload.entries if e.line_id not in by_id]
            if unknown:
                raise NotFoundError("Physical count line", unknown[0])

            entered = [by_id[entry.line_id] for entry in payload.entries]
            grains = [
                BalanceGrain(
                    item_id=line.item_id,
                    warehouse_id=count.warehouse_id,
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                )
                for line in entered
            ]
            items = await self.inventory.item_facts([line.item_id for line in entered])
            preview = await FifoCostPreview.load(self.inventory, self.layers, grains)

            now = utcnow()
            for entry, line, grain in zip(payload.entries, entered, grains, strict=True):
                counted = to_quantity(entry.counted_quantity)
                variance = to_quantity(counted - Decimal(line.system_quantity))
                line.counted_quantity = counted
                line.variance_quantity = variance
                line.variance_value = self._variance_value(
                    variance,
                    preview.line_cost(
                        grain,
                        variance,
                        supplied=Decimal(line.unit_cost),
                        fallback_cost=(
                            items[line.item_id].standard_cost
                            if line.item_id in items
                            else Decimal("0")
                        ),
                    ).total_cost,
                )
                line.counted_at = now
                line.counted_by_id = self.tenant.user_id
                if entry.notes is not None:
                    line.notes = entry.notes

            # Sessions disable autoflush. The completion query must see the
            # entries just recorded, or the last entered line remains pending.
            await self.session.flush()
            remaining = await self.line_repo.uncounted_count(count_id)
            previous = count.status
            if remaining == 0:
                COUNT_FLOW.assert_transition(count.status, PhysicalCountStatus.COUNTED)
                count.status = PhysicalCountStatus.COUNTED
                count.counted_at = now
            self.repo.touch(count)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_COUNT,
                entity_id=count.id,
                entity_code=count.code,
                summary=f"Recorded {len(payload.entries)} counted lines on {count.code}",
                user_id=self.tenant.user_id,
                context={"entered": len(payload.entries), "uncounted_remaining": remaining},
            )
            if count.status is not previous:
                await self._record_transition(count, previous, count.status, None)
        return await self.get_count(count_id)

    async def confirm(self, count_id: uuid.UUID) -> PhysicalCountRead:
        """Post the variance.

        The snapshot on each line is **not** what the variance is posted from.
        It was taken when the sheet was created, and between then and now other
        documents legitimately moved stock - a receipt was booked, an issue was
        made. Posting ``counted - snapshot`` would silently reverse every one of
        those movements and call it a count variance.

        So the current quantity is re-read under the same row lock the posting
        uses, and the movement posted is ``counted - current``. The snapshot is
        kept on the line, and where it disagrees with the locked value the audit
        entry records the drift.

        ASSUMPTION: a line whose stock moved between the snapshot and the posting
        is still posted, with the drift recorded, rather than refused pending a
        recount. The count is the physical fact and the system should end up
        agreeing with it; a long drift list is the signal that the warehouse was
        not frozen and the count is worth repeating. Refusing instead would make
        the sheet unfinishable in any warehouse that is still working.

        The **value** of the variance is taken from the postings, not computed
        beside them. Each line's ``variance_value`` and the sheet's total are
        restated from the ledger rows the engine wrote, so what a count reports
        as shrinkage is the number that actually left the balance sheet. They
        used to be ``variance_quantity x unit_cost`` against a snapshot average,
        which under FIFO is a different figure entirely: a sheet 60 units short
        of a position holding 100 @ 500 and 900 @ 10 reported 3 540 of shrinkage
        and posted 30 000 of it.

        ASSUMPTION: a counted line whose stock drifted back to the counted
        quantity posts nothing and therefore reports a variance value of zero,
        even though ``variance_quantity`` - which is measured against the
        *snapshot* and is left alone - is not zero. Nothing was written off, so
        nothing is reported as written off; the drift list below is where that
        line is explained.
        """
        async with transaction(self.session):
            count = await self._locked(count_id)
            COUNT_FLOW.assert_transition(count.status, PhysicalCountStatus.CONFIRMED)
            assert_warehouse_scope(self.tenant, count.warehouse_id)

            lines = await self.line_repo.all_for_count(count_id)
            counted_lines = [line for line in lines if line.counted_quantity is not None]
            if not counted_lines:
                raise BusinessRuleError(
                    "No line on this sheet has been counted.",
                    details={"count_code": count.code},
                )

            grains = [
                BalanceGrain(
                    item_id=line.item_id,
                    warehouse_id=count.warehouse_id,
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                )
                for line in counted_lines
            ]
            locked = await self.engine.lock(grains)

            movements: list[MovementRequest] = []
            # Parallel to ``movements``: the line each one came from, so the
            # ledger rows can be paired back to the lines that produced them.
            moved_lines: list[PhysicalCountLine] = []
            drifted: list[dict[str, Any]] = []
            variance_lines = 0

            for line, grain in zip(counted_lines, grains, strict=True):
                current = Decimal(locked[grain].on_hand_quantity)
                counted = Decimal(line.counted_quantity or 0)
                delta = to_quantity(counted - current)

                if current != Decimal(line.system_quantity):
                    drifted.append(
                        {
                            "line_number": line.line_number,
                            "item_id": line.item_id,
                            "snapshot_quantity": Decimal(line.system_quantity),
                            "current_quantity": current,
                            "counted_quantity": counted,
                        }
                    )

                if delta == 0:
                    # Nothing to post, so nothing was written off. Whatever the
                    # sheet estimated against the snapshot, the ledger booked
                    # zero and the line says zero.
                    line.variance_value = Decimal("0")
                    continue

                variance_lines += 1
                moved_lines.append(line)
                movements.append(
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=count.warehouse_id,
                        location_id=line.location_id,
                        batch_id=line.batch_id,
                        quantity=abs(delta),
                        transaction_type=(
                            TransactionType.ADJUSTMENT_INCREASE
                            if delta > 0
                            else TransactionType.ADJUSTMENT_DECREASE
                        ),
                        reference_type=AUDIT_ENTITY_COUNT,
                        reference_id=count.id,
                        reference_code=count.code,
                        reason=f"{AdjustmentReason.COUNT_VARIANCE}: {count.reason}"[:255],
                        unit_cost=Decimal(line.unit_cost),
                    )
                )

            variance_value = Decimal("0")
            if movements:
                transactions, _ = await self.engine.post_with_balances(movements, prelocked=locked)
                variance_value = self._restate_variance(moved_lines, transactions)

            previous = count.status
            count.status = PhysicalCountStatus.CONFIRMED
            count.confirmed_at = utcnow()
            count.confirmed_by_id = self.tenant.user_id
            count.variance_line_count = variance_lines
            count.variance_value = to_money(variance_value)
            self.repo.touch(count)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.STOCK_ADJUSTMENT,
                entity_type=AUDIT_ENTITY_COUNT,
                entity_id=count.id,
                entity_code=count.code,
                summary=(
                    f"Confirmed stock count {count.code}: {variance_lines} variance lines, "
                    f"net value {count.variance_value}"
                ),
                user_id=self.tenant.user_id,
                context={
                    "variance_line_count": variance_lines,
                    "variance_value": count.variance_value,
                    "counted_lines": len(counted_lines),
                    # Positions that moved between the snapshot and the posting.
                    # Not an error - but if this list is long, the warehouse was
                    # not frozen and the count is worth repeating.
                    "drifted_positions": drifted or None,
                },
            )
            await self._record_transition(
                count, previous, PhysicalCountStatus.CONFIRMED, count.reason
            )
        return await self.get_count(count_id)

    async def recount(self, count_id: uuid.UUID, reason: str) -> PhysicalCountRead:
        async with transaction(self.session):
            count = await self._locked(count_id)
            COUNT_FLOW.assert_transition(count.status, PhysicalCountStatus.IN_PROGRESS)
            previous = count.status
            count.status = PhysicalCountStatus.IN_PROGRESS
            count.counted_at = None
            self.repo.touch(count)
            await self.session.flush()
            await self._record_transition(count, previous, PhysicalCountStatus.IN_PROGRESS, reason)
        return await self.get_count(count_id)

    async def cancel(self, count_id: uuid.UUID, reason: str) -> PhysicalCountRead:
        async with transaction(self.session):
            count = await self._locked(count_id)
            COUNT_FLOW.assert_transition(count.status, PhysicalCountStatus.CANCELLED)
            previous = count.status
            count.status = PhysicalCountStatus.CANCELLED
            self.repo.touch(count)
            await self.session.flush()
            await self._record_transition(count, previous, PhysicalCountStatus.CANCELLED, reason)
        return await self.get_count(count_id)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _variance_value(variance_quantity: Decimal, magnitude: Decimal) -> Decimal:
        """A costed variance carries the sign of the quantity that caused it.

        Every costing in this module returns a magnitude - the direction lives
        on the movement, not on the money - so the one place that turns it back
        into a signed shrinkage or a signed gain is here.
        """
        value = to_money(magnitude)
        return -value if variance_quantity < 0 else value

    def _restate_variance(
        self,
        lines: Sequence[PhysicalCountLine],
        transactions: Sequence[InventoryTransaction],
    ) -> Decimal:
        """Write what the ledger booked onto the lines, and total it.

        ``transactions`` come back in the order the movements were given, which
        is the order ``lines`` produced them; ``strict=True`` is what proves the
        two lists stayed parallel. ``total_cost`` on a ledger row is a
        magnitude - ``quantity`` carries the direction - so the sign is taken
        from the movement rather than from the money, and the sheet's total is a
        signed net exactly as it was before.
        """
        total = Decimal("0")
        for line, txn in zip(lines, transactions, strict=True):
            value = self._variance_value(Decimal(txn.quantity), abs(Decimal(txn.total_cost)))
            line.variance_value = value
            total += value
        return total

    async def _locked(self, count_id: uuid.UUID) -> PhysicalCount:
        """Serialisation point for every state change on this sheet.

        Confirming is the destructive one: two simultaneous confirms both read
        COUNTED, both pass ``assert_transition``, and the sheet is posted and
        audited twice.
        """
        count: PhysicalCount = await _lock_document(
            self.session, self.repo.lock_for_update, "Physical count", count_id
        )
        return count

    async def _record_transition(
        self,
        count: PhysicalCount,
        previous: PhysicalCountStatus,
        target: PhysicalCountStatus,
        reason: str | None,
    ) -> None:
        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ENTITY_COUNT,
            entity_id=count.id,
            entity_code=count.code,
            from_status=str(previous),
            to_status=str(target),
            user_id=self.tenant.user_id,
            reason=reason,
        )

    @staticmethod
    def _to_read(
        count: PhysicalCount, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> PhysicalCountRead:
        read = PhysicalCountRead.model_validate(count)
        warehouse = warehouses.get(count.warehouse_id)
        read.warehouse_code = warehouse.code if warehouse else None
        return read


# -----------------------------------------------------------------------------
# Bin move, scrap disposal and internal issue
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LinePosition:
    """What one document line names, for the checks every new document shares.

    ``locations`` pairs each bin the line names with whether stock is *put
    into* it - the one direction the retired-bin rule applies to, as everywhere
    else in this module.
    """

    line_number: int
    item_id: uuid.UUID
    batch_id: uuid.UUID | None
    locations: tuple[tuple[uuid.UUID | None, bool], ...]


async def _assert_document_lines(
    inventory: InventoryRepository,
    *,
    document: str,
    warehouse: WarehouseFacts,
    positions: Sequence[_LinePosition],
) -> dict[uuid.UUID, ItemFacts]:
    """Hold a document's lines to the master-data rules, against the line that breaks one.

    Three queries for the whole document - items, batches, bins - never one per
    line. The engine re-checks all of it under its locks when the document
    posts; this is the answer an operator gets while still looking at the line:

    * the item exists in this tenant;
    * ASSUMPTION: a serial-tracked item is refused outright, because none of
      these documents has anywhere to name the units it moves - the same
      refusal a stock transfer makes (docs/assumptions.md section 25);
    * a batch-tracked item names its batch, and the batch is this item's - a
      batch-less line would draw on a batch-less position that holds nothing;
    * every bin is a live bin of this tenant, in this warehouse, and a bin
      that takes stock in is active.
    """
    items = await inventory.item_facts([position.item_id for position in positions])
    batches = await inventory.batch_owners(
        [position.batch_id for position in positions if position.batch_id is not None]
    )
    locations = await inventory.location_facts(
        [
            location_id
            for position in positions
            for location_id, _ in position.locations
            if location_id is not None
        ]
    )
    for position in positions:
        number = position.line_number
        item = items.get(position.item_id)
        if item is None:
            raise NotFoundError("Item", position.item_id)
        if item.is_serial_tracked:
            raise BusinessRuleError(
                f"Line {number}: {item.code} is serial tracked and cannot be moved by {document}: "
                "there is no contract for naming the units on this path.",
                code=ErrorCode.SERIAL_REQUIRED,
                details={"line_number": number, "item_code": item.code},
            )
        if position.batch_id is None:
            if item.is_batch_tracked:
                raise BusinessRuleError(
                    f"Line {number}: {item.code} is batch tracked, so the batch must be named.",
                    code=ErrorCode.BATCH_REQUIRED,
                    details={"line_number": number, "item_code": item.code},
                )
        else:
            owner = batches.get(position.batch_id)
            if owner is None:
                raise NotFoundError("Stock batch", position.batch_id)
            if owner[0] != position.item_id:
                raise BusinessRuleError(
                    f"Line {number}: batch {owner[1]} belongs to a different item.",
                    details={"line_number": number, "batch_id": str(position.batch_id)},
                )
        for location_id, receiving in position.locations:
            if location_id is None:
                continue
            location = locations.get(location_id)
            if location is None:
                raise NotFoundError("Storage location", location_id)
            if location.warehouse_id != warehouse.id:
                raise BusinessRuleError(
                    f"Line {number}: storage location {location.code} is not in "
                    f"warehouse {warehouse.code}.",
                    details={
                        "line_number": number,
                        "location_id": str(location_id),
                        "location_code": location.code,
                        "warehouse_code": warehouse.code,
                    },
                )
            if receiving and not location.is_active:
                raise BusinessRuleError(
                    f"Line {number}: "
                    + inactive_record("Storage location", location.code, "cannot receive stock"),
                    details={
                        "line_number": number,
                        "location_id": str(location_id),
                        "location_code": location.code,
                    },
                )
    return items


async def _document_warehouse(
    inventory: InventoryRepository, warehouse_id: uuid.UUID, *, receiving: bool
) -> WarehouseFacts:
    warehouses = await inventory.warehouse_facts([warehouse_id])
    warehouse = warehouses.get(warehouse_id)
    if warehouse is None:
        raise NotFoundError("Warehouse", warehouse_id)
    if receiving and not warehouse.is_active:
        raise BusinessRuleError(
            inactive_record("Warehouse", warehouse.code, "cannot receive stock"),
            details={"warehouse_code": warehouse.code},
        )
    return warehouse


def _business_time(on: date) -> datetime:
    """A document date as the business time of the movements it posts.

    The time of day is the posting's own, the same choice a stock adjustment
    makes, so a document keyed late is dated when it happened and movements
    posted together keep the order they were posted in.
    """
    return datetime.combine(on, utcnow().timetz())


class BinMoveService:
    """Putaway and re-slotting: stock moved between bins of one warehouse.

    Posted the moment it is raised. Each line is a ``TRANSFER_OUT`` at the
    source bin and a ``TRANSFER_IN`` at the destination, in one engine batch, so
    both positions are locked together and the inbound leg re-opens exactly the
    FIFO layers the outbound leg consumed (docs/assumptions.md §E8-1).
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = BinMoveRepository(session, tenant)
        self.line_repo = BinMoveLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    async def list_moves(
        self, filters: BinMoveFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[BinMoveReadWithCost], OffsetPagination]:
        rows, pagination = await paginate(
            self.session, self.repo.build_list_stmt(filters, sort), params
        )
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        return [self._to_read(row, warehouses) for row in rows], pagination

    async def get_move(self, bin_move_id: uuid.UUID) -> BinMoveReadWithCost:
        move = await self.repo.get_with_lines(bin_move_id)
        if move is None:
            raise NotFoundError("Bin move", bin_move_id)
        warehouses = await self.inventory.warehouse_facts([move.warehouse_id])
        items = await self.inventory.item_facts([line.item_id for line in move.lines])
        read = self._to_read(move, warehouses)
        read.lines = [
            _with_item(BinMoveLineReadWithCost.model_validate(line), items.get(line.item_id))
            for line in move.lines
        ]
        await _label_operation_positions(self.inventory, read)
        return read

    async def create(self, payload: BinMoveCreate) -> BinMoveReadWithCost:
        async with transaction(self.session):
            assert_warehouse_scope(self.tenant, payload.warehouse_id)
            # Receiving: the destination bin takes stock in.
            warehouse = await _document_warehouse(
                self.inventory, payload.warehouse_id, receiving=True
            )
            await _assert_document_lines(
                self.inventory,
                document="a bin move",
                warehouse=warehouse,
                positions=[
                    _LinePosition(
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        locations=((line.from_location_id, False), (line.to_location_id, True)),
                    )
                    for number, line in enumerate(payload.lines, start=1)
                ],
            )

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.BIN_MOVE
            )
            move = BinMove(
                code=code,
                status=BinMoveStatus.DRAFT,
                warehouse_id=payload.warehouse_id,
                move_date=payload.move_date or today(),
                reason=payload.reason,
                notes=payload.notes,
                line_count=len(payload.lines),
                total_quantity=to_quantity(sum(line.quantity for line in payload.lines)),
            )
            self.repo.add(move)
            await self.session.flush()
            lines = [
                self.line_repo.add(
                    BinMoveLine(
                        bin_move_id=move.id,
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        from_location_id=line.from_location_id,
                        to_location_id=line.to_location_id,
                        quantity=to_quantity(line.quantity),
                    )
                )
                for number, line in enumerate(payload.lines, start=1)
            ]
            await self.session.flush()
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_BIN_MOVE,
                entity_id=move.id,
                entity_code=move.code,
                summary=f"Raised bin move {move.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    move, ["code", "warehouse_id", "move_date", "reason", "line_count"]
                ),
            )

            # ASSUMPTION: a bin move is a TRANSFER_OUT / TRANSFER_IN pair within
            # one warehouse, posted in one batch with the inbound leg carrying
            # the outbound leg's layers - no transit pool and no new movement
            # type. docs/assumptions.md §E8-1.
            occurred_at = _business_time(move.move_date)
            movements: list[MovementRequest] = []
            for line in lines:
                common: dict[str, Any] = {
                    "item_id": line.item_id,
                    "warehouse_id": move.warehouse_id,
                    "batch_id": line.batch_id,
                    "quantity": Decimal(line.quantity),
                    "reference_type": AUDIT_ENTITY_BIN_MOVE,
                    "reference_id": move.id,
                    "reference_code": move.code,
                    "reason": move.reason,
                    "occurred_at": occurred_at,
                }
                outbound = len(movements)
                movements.append(
                    MovementRequest(
                        location_id=line.from_location_id,
                        transaction_type=TransactionType.TRANSFER_OUT,
                        **common,
                    )
                )
                movements.append(
                    MovementRequest(
                        location_id=line.to_location_id,
                        transaction_type=TransactionType.TRANSFER_IN,
                        carries_cost_of=outbound,
                        **common,
                    )
                )
            posted = await self.engine.post(movements)

            total_value = Decimal("0")
            # Every other row is an outbound leg, and it is what the stock cost.
            for line, outbound_txn in zip(lines, posted[::2], strict=True):
                line.unit_cost = to_unit_price(outbound_txn.unit_cost)
                line.total_cost = to_money(abs(Decimal(outbound_txn.total_cost)))
                total_value += line.total_cost
            move.total_value = to_money(total_value)

            BIN_MOVE_FLOW.assert_transition(move.status, BinMoveStatus.POSTED)
            move.status = BinMoveStatus.POSTED
            move.posted_at = utcnow()
            self.repo.touch(move)
            await self.session.flush()
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_BIN_MOVE,
                entity_id=move.id,
                entity_code=move.code,
                from_status=str(BinMoveStatus.DRAFT),
                to_status=str(BinMoveStatus.POSTED),
                user_id=self.tenant.user_id,
                reason=move.reason,
            )
            move_id = move.id
        return await self.get_move(move_id)

    @staticmethod
    def _to_read(move: BinMove, warehouses: dict[uuid.UUID, WarehouseFacts]) -> BinMoveReadWithCost:
        _declare_lines_empty_if_unloaded(move)
        read = BinMoveReadWithCost.model_validate(move)
        warehouse = warehouses.get(move.warehouse_id)
        read.warehouse_code = warehouse.code if warehouse else None
        return read


class ScrapDisposalService:
    """Stock out of the SCRAP pool for good: sold, recycled or written off.

    Posted the moment it is raised, one ``SCRAP_DISPOSAL`` per line. Each is
    valued from the scrap cost layers it consumes - what the scrap cost when it
    was scrapped - which is the figure ``realised_amount`` is weighed against.
    Scrap carries no book value, so a disposal changes no stock value: the
    write-off happened when the stock was scrapped.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = ScrapDisposalRepository(session, tenant)
        self.line_repo = ScrapDisposalLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    async def list_disposals(
        self, filters: ScrapDisposalFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ScrapDisposalReadWithCost], OffsetPagination]:
        rows, pagination = await paginate(
            self.session, self.repo.build_list_stmt(filters, sort), params
        )
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        return [self._to_read(row, warehouses) for row in rows], pagination

    async def get_disposal(self, disposal_id: uuid.UUID) -> ScrapDisposalReadWithCost:
        disposal = await self.repo.get_with_lines(disposal_id)
        if disposal is None:
            raise NotFoundError("Scrap disposal", disposal_id)
        warehouses = await self.inventory.warehouse_facts([disposal.warehouse_id])
        items = await self.inventory.item_facts([line.item_id for line in disposal.lines])
        read = self._to_read(disposal, warehouses)
        read.lines = [
            _with_item(ScrapDisposalLineReadWithCost.model_validate(line), items.get(line.item_id))
            for line in disposal.lines
        ]
        await _label_operation_positions(self.inventory, read)
        return read

    async def create(self, payload: ScrapDisposalCreate) -> ScrapDisposalReadWithCost:
        async with transaction(self.session):
            assert_warehouse_scope(self.tenant, payload.warehouse_id)
            # Not receiving: scrap may leave a warehouse that has been retired.
            warehouse = await _document_warehouse(
                self.inventory, payload.warehouse_id, receiving=False
            )
            await _assert_document_lines(
                self.inventory,
                document="a scrap disposal",
                warehouse=warehouse,
                positions=[
                    _LinePosition(
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        locations=((line.location_id, False),),
                    )
                    for number, line in enumerate(payload.lines, start=1)
                ],
            )

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.SCRAP_DISPOSAL
            )
            disposal = ScrapDisposal(
                code=code,
                status=ScrapDisposalStatus.DRAFT,
                warehouse_id=payload.warehouse_id,
                disposal_date=payload.disposal_date or today(),
                disposal_kind=payload.disposal_kind,
                buyer_reference=payload.buyer_reference,
                realised_amount=to_money(payload.realised_amount),
                reason=payload.reason,
                notes=payload.notes,
                line_count=len(payload.lines),
                total_quantity=to_quantity(sum(line.quantity for line in payload.lines)),
            )
            self.repo.add(disposal)
            await self.session.flush()
            lines = [
                self.line_repo.add(
                    ScrapDisposalLine(
                        scrap_disposal_id=disposal.id,
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        location_id=line.location_id,
                        quantity=to_quantity(line.quantity),
                    )
                )
                for number, line in enumerate(payload.lines, start=1)
            ]
            await self.session.flush()
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_SCRAP_DISPOSAL,
                entity_id=disposal.id,
                entity_code=disposal.code,
                summary=(
                    f"Raised scrap disposal {disposal.code} ({disposal.disposal_kind}, "
                    f"realised {disposal.realised_amount})"
                ),
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    disposal,
                    [
                        "code",
                        "warehouse_id",
                        "disposal_date",
                        "disposal_kind",
                        "buyer_reference",
                        "realised_amount",
                        "reason",
                        "line_count",
                    ],
                ),
            )

            occurred_at = _business_time(disposal.disposal_date)
            posted = await self.engine.post(
                [
                    MovementRequest(
                        item_id=line.item_id,
                        warehouse_id=disposal.warehouse_id,
                        location_id=line.location_id,
                        batch_id=line.batch_id,
                        quantity=Decimal(line.quantity),
                        transaction_type=TransactionType.SCRAP_DISPOSAL,
                        # Named although it is the type's default source, so the
                        # ledger row reads "scrap" in its stock-state column.
                        stock_state=StockState.SCRAP,
                        reference_type=AUDIT_ENTITY_SCRAP_DISPOSAL,
                        reference_id=disposal.id,
                        reference_code=disposal.code,
                        reason=f"{disposal.disposal_kind}: {disposal.reason}"[:255],
                        occurred_at=occurred_at,
                    )
                    for line in lines
                ]
            )

            total_value = Decimal("0")
            for line, txn in zip(lines, posted, strict=True):
                line.unit_cost = to_unit_price(txn.unit_cost)
                line.total_cost = to_money(abs(Decimal(txn.total_cost)))
                total_value += line.total_cost
            disposal.total_value = to_money(total_value)

            SCRAP_DISPOSAL_FLOW.assert_transition(disposal.status, ScrapDisposalStatus.POSTED)
            disposal.status = ScrapDisposalStatus.POSTED
            disposal.posted_at = utcnow()
            self.repo.touch(disposal)
            await self.session.flush()
            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_SCRAP_DISPOSAL,
                entity_id=disposal.id,
                entity_code=disposal.code,
                from_status=str(ScrapDisposalStatus.DRAFT),
                to_status=str(ScrapDisposalStatus.POSTED),
                user_id=self.tenant.user_id,
                reason=disposal.reason,
            )
            disposal_id = disposal.id
        return await self.get_disposal(disposal_id)

    @staticmethod
    def _to_read(
        disposal: ScrapDisposal, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> ScrapDisposalReadWithCost:
        _declare_lines_empty_if_unloaded(disposal)
        read = ScrapDisposalReadWithCost.model_validate(disposal)
        warehouse = warehouses.get(disposal.warehouse_id)
        read.warehouse_code = warehouse.code if warehouse else None
        return read


class InternalIssueService:
    """Stock issued to a department or cost centre for its own consumption.

    The approval control is the stock adjustment's, measured the same way: a
    draft is priced off the FIFO layers it will consume, and one worth more than
    :data:`INTERNAL_ISSUE_APPROVAL_VALUE` cannot take the DRAFT -> POSTED edge.
    It is submitted, and a second person approves it - approving is what posts
    it. A draft that looked small and turns out, at posting, to cost more than
    the threshold is refused and nothing reaches the ledger.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = InternalIssueRepository(session, tenant)
        self.line_repo = InternalIssueLineRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        self.layers = CostLayerRepository(session, tenant)
        self.engine = MovementEngine(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_issues(
        self, filters: InternalIssueFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[InternalIssueReadWithCost], OffsetPagination]:
        rows, pagination = await paginate(
            self.session, self.repo.build_list_stmt(filters, sort), params
        )
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        return [self._to_read(row, warehouses) for row in rows], pagination

    async def get_issue(self, issue_id: uuid.UUID) -> InternalIssueReadWithCost:
        issue = await self.repo.get_with_lines(issue_id)
        if issue is None:
            raise NotFoundError("Internal issue", issue_id)
        warehouses = await self.inventory.warehouse_facts([issue.warehouse_id])
        items = await self.inventory.item_facts([line.item_id for line in issue.lines])
        read = self._to_read(issue, warehouses)
        read.lines = [
            _with_item(InternalIssueLineReadWithCost.model_validate(line), items.get(line.item_id))
            for line in issue.lines
        ]
        await _label_operation_positions(self.inventory, read)
        return read

    # --- writes -------------------------------------------------------------

    async def create(self, payload: InternalIssueCreate) -> InternalIssueReadWithCost:
        async with transaction(self.session):
            assert_warehouse_scope(self.tenant, payload.warehouse_id)
            # Not receiving: stock leaves the warehouse.
            warehouse = await _document_warehouse(
                self.inventory, payload.warehouse_id, receiving=False
            )
            items = await _assert_document_lines(
                self.inventory,
                document="an internal issue",
                warehouse=warehouse,
                positions=[
                    _LinePosition(
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        locations=((line.location_id, False),),
                    )
                    for number, line in enumerate(payload.lines, start=1)
                ],
            )
            if payload.department_id is not None:
                await require_department(self.session, self.tenant, payload.department_id)
            if payload.cost_centre_id is not None:
                await require_cost_centre(self.session, self.tenant, payload.cost_centre_id)

            code = await next_document_number(
                self.session, self.tenant.company_id, DocTypes.INTERNAL_ISSUE
            )
            issue = InternalIssue(
                code=code,
                status=InternalIssueStatus.DRAFT,
                warehouse_id=payload.warehouse_id,
                issue_date=payload.issue_date or today(),
                department_id=payload.department_id,
                cost_centre_id=payload.cost_centre_id,
                reason=payload.reason,
                notes=payload.notes,
                line_count=len(payload.lines),
                total_quantity=to_quantity(sum(line.quantity for line in payload.lines)),
            )
            self.repo.add(issue)
            await self.session.flush()

            grains = [
                BalanceGrain(
                    item_id=line.item_id,
                    warehouse_id=payload.warehouse_id,
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                )
                for line in payload.lines
            ]
            preview = await FifoCostPreview.load(self.inventory, self.layers, grains)
            total_value = Decimal("0")
            for number, (line, grain) in enumerate(
                zip(payload.lines, grains, strict=True), start=1
            ):
                # Negative: an issue draws on the queue, oldest layer first.
                cost = preview.line_cost(
                    grain,
                    -to_quantity(line.quantity),
                    supplied=None,
                    fallback_cost=items[line.item_id].standard_cost,
                )
                total_value += cost.total_cost
                self.line_repo.add(
                    InternalIssueLine(
                        internal_issue_id=issue.id,
                        line_number=number,
                        item_id=line.item_id,
                        batch_id=line.batch_id,
                        location_id=line.location_id,
                        quantity=to_quantity(line.quantity),
                        unit_cost=cost.unit_cost,
                        total_cost=cost.total_cost,
                        notes=line.notes,
                    )
                )
            self._set_value(issue, total_value)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_INTERNAL_ISSUE,
                entity_id=issue.id,
                entity_code=issue.code,
                summary=f"Drafted internal issue {issue.code} (value {issue.total_value})",
                user_id=self.tenant.user_id,
                new_values=snapshot(
                    issue,
                    [
                        "code",
                        "warehouse_id",
                        "issue_date",
                        "department_id",
                        "cost_centre_id",
                        "reason",
                        "total_value",
                        "requires_approval",
                        "line_count",
                    ],
                ),
            )
            issue_id = issue.id
        return await self.get_issue(issue_id)

    async def submit(self, issue_id: uuid.UUID) -> InternalIssueReadWithCost:
        """Send a draft for approval, re-priced against the layers as they are now.

        Re-priced for the reason :meth:`StockAdjustmentService.submit` gives: a
        draft whose real cost has risen past the threshold since it was keyed
        would otherwise be refused by :meth:`post` for needing an approval and
        refused here for not needing one.
        """
        async with transaction(self.session):
            await self._locked(issue_id)
            issue = await self._with_lines(issue_id)
            INTERNAL_ISSUE_FLOW.assert_transition(
                issue.status, InternalIssueStatus.PENDING_APPROVAL
            )
            assert_warehouse_scope(self.tenant, issue.warehouse_id)
            await self._reprice(issue)
            if not issue.requires_approval:
                raise BusinessRuleError(
                    f"Internal issue {issue.code} is below the approval value and can be "
                    "posted directly.",
                    details={
                        "total_value": str(issue.total_value),
                        "threshold": str(INTERNAL_ISSUE_APPROVAL_VALUE),
                    },
                )
            issue.status = InternalIssueStatus.PENDING_APPROVAL
            self.repo.touch(issue)
            await self.session.flush()
            await self._record_transition(
                issue, InternalIssueStatus.DRAFT, InternalIssueStatus.PENDING_APPROVAL, None
            )
        return await self.get_issue(issue_id)

    async def post(self, issue_id: uuid.UUID) -> InternalIssueReadWithCost:
        """Post a draft that is under the approval value.

        The threshold is checked twice, the second time against what the ledger
        actually charged - :meth:`StockAdjustmentService.post` explains why the
        second check is the one that makes the control real.
        """
        async with transaction(self.session):
            await self._locked(issue_id)
            issue = await self._with_lines(issue_id)
            INTERNAL_ISSUE_FLOW.assert_transition(issue.status, InternalIssueStatus.POSTED)
            if issue.status is not InternalIssueStatus.DRAFT:
                # PENDING_APPROVAL -> POSTED is the approver's edge, not this one.
                raise BusinessRuleError(
                    f"Internal issue {issue.code} is waiting for approval; approving it posts it.",
                    code=ErrorCode.APPROVAL_REQUIRED,
                    status_code=422,
                )
            assert_warehouse_scope(self.tenant, issue.warehouse_id)
            if issue.requires_approval:
                raise self._approval_required(issue)
            await self._post_lines(issue)
            if issue.total_value > INTERNAL_ISSUE_APPROVAL_VALUE:
                raise self._approval_required(issue)
            await self._finish_posting(issue, InternalIssueStatus.DRAFT, AuditAction.STATUS_CHANGE)
        return await self.get_issue(issue_id)

    async def approve(self, issue_id: uuid.UUID) -> InternalIssueReadWithCost:
        """Approve a pending issue, which posts it.

        Posted at whatever the layers now charge: a person looked at it and
        signed it, and refusing it here would make an approved document
        unpostable because somebody else moved stock. The restated value is
        recorded either way.
        """
        async with transaction(self.session):
            await self._locked(issue_id)
            issue = await self._with_lines(issue_id)
            INTERNAL_ISSUE_FLOW.assert_transition(issue.status, InternalIssueStatus.POSTED)
            if issue.status is not InternalIssueStatus.PENDING_APPROVAL:
                raise BusinessRuleError(
                    f"Internal issue {issue.code} is not waiting for approval.",
                    details={"status": str(issue.status)},
                )
            assert_warehouse_scope(self.tenant, issue.warehouse_id)
            self._assert_separate_approver(issue)
            await self._post_lines(issue)
            issue.approved_by_id = self.tenant.user_id
            issue.approved_at = utcnow()
            await self._finish_posting(
                issue, InternalIssueStatus.PENDING_APPROVAL, AuditAction.APPROVE
            )
        return await self.get_issue(issue_id)

    async def reject(self, issue_id: uuid.UUID, reason: str) -> InternalIssueReadWithCost:
        """An approver refuses a pending issue. It ends cancelled, with the reason."""
        async with transaction(self.session):
            issue = await self._locked(issue_id)
            self._assert_separate_approver(issue)
            if issue.status is not InternalIssueStatus.PENDING_APPROVAL:
                raise BusinessRuleError(
                    f"Internal issue {issue.code} is not waiting for approval.",
                    details={"status": str(issue.status)},
                )
            INTERNAL_ISSUE_FLOW.assert_transition(issue.status, InternalIssueStatus.CANCELLED)
            issue.status = InternalIssueStatus.CANCELLED
            issue.decision_note = reason
            self.repo.touch(issue)
            await self.session.flush()
            await self._record_transition(
                issue,
                InternalIssueStatus.PENDING_APPROVAL,
                InternalIssueStatus.CANCELLED,
                reason,
                action=AuditAction.REJECT,
            )
        return await self.get_issue(issue_id)

    async def cancel(self, issue_id: uuid.UUID, reason: str) -> InternalIssueReadWithCost:
        async with transaction(self.session):
            issue = await self._locked(issue_id)
            INTERNAL_ISSUE_FLOW.assert_transition(issue.status, InternalIssueStatus.CANCELLED)
            previous = issue.status
            issue.status = InternalIssueStatus.CANCELLED
            issue.decision_note = reason
            self.repo.touch(issue)
            await self.session.flush()
            await self._record_transition(
                issue, previous, InternalIssueStatus.CANCELLED, reason, action=AuditAction.CANCEL
            )
        return await self.get_issue(issue_id)

    # --- helpers ------------------------------------------------------------

    async def _locked(self, issue_id: uuid.UUID) -> InternalIssue:
        """Serialisation point for every state change on this document.

        Without it two approvals both read PENDING_APPROVAL and both post the
        issue to the ledger; the balance locks only make the two postings
        sequential.
        """
        issue: InternalIssue = await _lock_document(
            self.session, self.repo.lock_for_update, "Internal issue", issue_id
        )
        return issue

    async def _with_lines(self, issue_id: uuid.UUID) -> InternalIssue:
        issue = await self.repo.get_with_lines(issue_id)
        if issue is None:  # pragma: no cover - the lock already found it
            raise NotFoundError("Internal issue", issue_id)
        return issue

    def _assert_separate_approver(self, issue: InternalIssue) -> None:
        # ASSUMPTION: the person who raised an issue may not approve it, unless
        # they are a platform administrator - the project-wide segregation of
        # duties (docs/assumptions.md section 2), as stock adjustments apply it.
        if self.tenant.is_superuser:
            return
        if issue.created_by_id == self.tenant.user_id:
            raise BusinessRuleError(
                "An internal issue must be approved or rejected by someone other than the "
                "person who raised it.",
                details={"internal_issue_code": issue.code},
            )

    @staticmethod
    def _approval_required(issue: InternalIssue) -> BusinessRuleError:
        return BusinessRuleError(
            f"Internal issue {issue.code} is worth {issue.total_value} against the cost "
            "layers it consumes and must be approved before it can post.",
            code=ErrorCode.APPROVAL_REQUIRED,
            status_code=422,
            details={
                "total_value": str(issue.total_value),
                "threshold": str(INTERNAL_ISSUE_APPROVAL_VALUE),
            },
        )

    @staticmethod
    def _set_value(issue: InternalIssue, total: Decimal) -> None:
        """The value and the approval flag, always set together."""
        issue.total_value = to_money(total)
        issue.requires_approval = issue.total_value > INTERNAL_ISSUE_APPROVAL_VALUE

    async def _reprice(self, issue: InternalIssue) -> None:
        lines = sorted(issue.lines, key=lambda line: line.line_number)
        grains = [
            BalanceGrain(
                item_id=line.item_id,
                warehouse_id=issue.warehouse_id,
                location_id=line.location_id,
                batch_id=line.batch_id,
            )
            for line in lines
        ]
        items = await self.inventory.item_facts([line.item_id for line in lines])
        preview = await FifoCostPreview.load(self.inventory, self.layers, grains)
        total = Decimal("0")
        for line, grain in zip(lines, grains, strict=True):
            item = items.get(line.item_id)
            cost = preview.line_cost(
                grain,
                -Decimal(line.quantity),
                supplied=None,
                fallback_cost=item.standard_cost if item else Decimal("0"),
            )
            line.unit_cost = cost.unit_cost
            line.total_cost = cost.total_cost
            self.line_repo.touch(line)
            total += cost.total_cost
        self._set_value(issue, total)
        self.repo.touch(issue)

    async def _post_lines(self, issue: InternalIssue) -> None:
        """Post one MATERIAL_ISSUE per line and restate the document from the ledger."""
        occurred_at = _business_time(issue.issue_date)
        posted = await self.engine.post(
            [
                MovementRequest(
                    item_id=line.item_id,
                    warehouse_id=issue.warehouse_id,
                    location_id=line.location_id,
                    batch_id=line.batch_id,
                    quantity=Decimal(line.quantity),
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    reference_type=AUDIT_ENTITY_INTERNAL_ISSUE,
                    reference_id=issue.id,
                    reference_code=issue.code,
                    reason=f"Internal issue: {issue.reason}"[:255],
                    occurred_at=occurred_at,
                )
                for line in issue.lines
            ]
        )
        total = Decimal("0")
        for line, txn in zip(issue.lines, posted, strict=True):
            line.unit_cost = to_unit_price(txn.unit_cost)
            line.total_cost = to_money(abs(Decimal(txn.total_cost)))
            self.line_repo.touch(line)
            total += line.total_cost
        self._set_value(issue, total)

    async def _finish_posting(
        self, issue: InternalIssue, previous: InternalIssueStatus, action: AuditAction
    ) -> None:
        issue.status = InternalIssueStatus.POSTED
        issue.posted_at = utcnow()
        self.repo.touch(issue)
        await self.session.flush()
        await self._record_transition(
            issue, previous, InternalIssueStatus.POSTED, None, action=action
        )

    async def _record_transition(
        self,
        issue: InternalIssue,
        previous: InternalIssueStatus,
        target: InternalIssueStatus,
        reason: str | None,
        *,
        action: AuditAction = AuditAction.STATUS_CHANGE,
    ) -> None:
        await record_status_change(
            self.session,
            company_id=self.tenant.company_id,
            entity_type=AUDIT_ENTITY_INTERNAL_ISSUE,
            entity_id=issue.id,
            entity_code=issue.code,
            from_status=str(previous),
            to_status=str(target),
            user_id=self.tenant.user_id,
            action=action,
            reason=reason,
        )

    @staticmethod
    def _to_read(
        issue: InternalIssue, warehouses: dict[uuid.UUID, WarehouseFacts]
    ) -> InternalIssueReadWithCost:
        _declare_lines_empty_if_unloaded(issue)
        read = InternalIssueReadWithCost.model_validate(issue)
        warehouse = warehouses.get(issue.warehouse_id)
        read.warehouse_code = warehouse.code if warehouse else None
        return read


_LineReadT = TypeVar(
    "_LineReadT",
    BinMoveLineReadWithCost,
    ScrapDisposalLineReadWithCost,
    InternalIssueLineReadWithCost,
)


async def _label_operation_positions(
    inventory: InventoryRepository,
    read: BinMoveReadWithCost | ScrapDisposalReadWithCost | InternalIssueReadWithCost,
) -> None:
    location_ids: set[uuid.UUID] = set()
    batch_ids = {line.batch_id for line in read.lines if line.batch_id is not None}
    for line in read.lines:
        positions = (
            (line.from_location_id, line.to_location_id)
            if isinstance(line, BinMoveLineReadWithCost)
            else (line.location_id,)
        )
        location_ids.update(value for value in positions if value is not None)
    locations, batches = await inventory.document_position_labels(location_ids, batch_ids)
    for line in read.lines:
        line.batch_code = batches.get(line.batch_id) if line.batch_id else None
        if isinstance(line, BinMoveLineReadWithCost):
            line.from_location_code = (
                locations.get(line.from_location_id) if line.from_location_id else None
            )
            line.to_location_code = (
                locations.get(line.to_location_id) if line.to_location_id else None
            )
        else:
            line.location_code = locations.get(line.location_id) if line.location_id else None


def _with_item(read: _LineReadT, item: ItemFacts | None) -> _LineReadT:
    read.item_code = item.code if item else None
    read.item_name = item.name if item else None
    return read


__all__ = [
    "ADJUSTMENT_APPROVAL_VALUE_THRESHOLD",
    "ADJUSTMENT_FLOW",
    "BIN_MOVE_FLOW",
    "COUNT_FLOW",
    "INTERNAL_ISSUE_APPROVAL_VALUE",
    "INTERNAL_ISSUE_FLOW",
    "LOCK_TIMEOUT_MS",
    "MAX_COUNT_LINES",
    "SCRAP_DISPOSAL_FLOW",
    "SCRAP_DRAWING_TYPES",
    "SCRAP_LANDING_TYPES",
    "SERIAL_FLOW",
    "SERIAL_IN_WAREHOUSE_STATUSES",
    "SERIAL_MOVEMENT_TARGETS",
    "SERIAL_RELOCATING_TYPES",
    "TARGET_QUALIFIED_TYPES",
    "TRANSFER_FLOW",
    "BalanceEffect",
    "BinMoveService",
    "FifoCostPreview",
    "InternalIssueService",
    "InventoryQueryService",
    "LineCost",
    "MovementEngine",
    "MovementRequest",
    "PhysicalCountService",
    "ScrapDisposalService",
    "StockAdjustmentService",
    "StockShortfall",
    "StockTransferService",
    "TraceabilityService",
    "Valuation",
    "assert_warehouse_scope",
    "carried_cost_layers",
    "change_stock_state",
    "draws_on_scrap",
    "find_shortfall",
    "get_available",
    "get_balance",
    "movement_effect",
    "movement_relocates_unit",
    "post_movement",
    "post_movements",
    "scrap_layers_for",
    "serial_transition",
    "signed_quantity",
    "state_change_type",
    "to_stock_value",
    "transfer_stock",
    "validate_movement",
    "value_movement",
]
