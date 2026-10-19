"""The reservation and reallocation engine.

Master rules section 13, database contract section 4.5. This module is the only
place ``inventory_balances.reserved_quantity`` is allowed to change, in the same
way :mod:`src.modules.inventory.service` is the only place ``on_hand_quantity``
is allowed to change. The two must never be able to disagree, and every rule
below exists to make disagreement impossible rather than merely unlikely.

The four invariants
-------------------
1. **A reservation takes the same row lock a movement does.** ``reserve`` locks
   the ``inventory_balances`` row through
   :meth:`~src.modules.inventory.service.MovementEngine.lock` - the identical
   primitive ``post_movement`` uses - and re-reads the quantities *after* the
   lock. Two orders racing for the last 100 units therefore queue on the same
   PostgreSQL row, and exactly one of them wins. A reservation engine with its
   own locking scheme would leave a window where a reservation and an issue each
   believed they had the stock.

2. **A reservation never writes a ledger row.** Nothing has moved. On-hand is
   unchanged, the goods are still on the shelf, and inventing an
   ``inventory_transaction`` for a promise would corrupt the one table that is
   supposed to answer "why is the balance what it is". The change is recorded in
   ``stock_reservations`` and in the audit log with
   :attr:`~src.modules.audit.models.AuditAction.RESERVATION_CHANGE`.

3. **Consumption is one transaction, not two.** ``consume`` decrements
   ``reserved_quantity`` and posts the real issue or dispatch in the same
   transaction. The order matters and is not arbitrary: the reservation must be
   released *before* the movement, because the movement engine refuses any
   posting that would leave ``reserved > on_hand``, and issuing against a
   still-standing reservation is exactly that.

4. **Reallocation is a release plus a reserve, never a stock update.** Section 13
   is explicit. Executing an approved request runs the same
   :func:`release`/:func:`reserve` primitives against one locked balance row, so
   the accounting is identical to a human doing it by hand - and auditable in
   the same way.

Lock ordering
-------------
Two lock classes are taken here: ``inventory_balances`` rows and
``stock_reservations`` rows. **The balance is always locked first**, and within
each class the rows are ordered by primary key (the movement engine and
:meth:`~src.modules.allocation.repository.StockReservationRepository.lock_reservations`
both do this). A reallocation touching a donor and a recipient therefore cannot
deadlock against one running the other way.

Availability is never cached
----------------------------
:func:`available_to_promise` reads the balance table on every call (section 34).
A cached availability that is thirty seconds stale is how two customers get
promised the same stock, and the cost of being right is a single indexed sum.

The configurable seam
---------------------
:class:`ReallocationPolicy` holds every ranking weight and every eligibility
threshold; :data:`DEFAULT_POLICY` is the shipped default. Master rules section 13
requires the allocation priority to be business-driven and configurable, so no
comparison in this module hardcodes a judgement - each one reads a policy field.
Changing how the business ranks donors is a change to one dataclass, and
:func:`rank_candidates` is pure, so the effect of that change is unit-testable
without a database.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    ErrorCode,
    InsufficientStockError,
    NotFoundError,
)
from src.core.ids import persisted_actor
from src.core.logging import get_logger
from src.core.observability import insufficient_stock_events
from src.core.pagination import PageParams, SortParams, paginate
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.core.types import ZERO, to_quantity
from src.modules.allocation.models import (
    DEFAULT_RESERVATION_PRIORITY,
    OPEN_RESERVATION_STATUSES,
    DemandType,
    ReallocationRequest,
    ReallocationStatus,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.allocation.repository import (
    CANDIDATE_POOL_LIMIT,
    ReallocationRequestRepository,
    ReservationFacts,
    StockReservationRepository,
)
from src.modules.allocation.schemas import (
    AvailableToPromiseRead,
    ReallocationCandidateRead,
    ReallocationFilters,
    ReallocationRequestCreate,
    ReallocationRequestRead,
    ReservationCreate,
    ReservationFilters,
    ReservationRead,
)
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change
from src.modules.inventory.models import InventoryBalance, TransactionType
from src.modules.inventory.repository import BalanceGrain, InventoryRepository, ItemFacts
from src.modules.inventory.service import (
    MovementEngine,
    MovementRequest,
    post_movement,
    post_movements,
)
from src.modules.inventory.service import get_available as inventory_available
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify

log = get_logger(__name__)

AUDIT_ENTITY_RESERVATION = "stock_reservation"
AUDIT_ENTITY_REALLOCATION = "reallocation_request"

#: How many stale reservations one housekeeping pass will expire. Bounded so the
#: sweep holds a predictable number of row locks for a predictable time; the job
#: runs again on its next tick rather than trying to catch up in one go.
MAX_EXPIRY_BATCH = 200


# -----------------------------------------------------------------------------
# State machines (section 73)
# -----------------------------------------------------------------------------

RESERVATION_FLOW: StateMachine[ReservationStatus] = StateMachine(
    entity="Stock reservation",
    initial=ReservationStatus.ACTIVE,
    transitions=(
        # The two self-edges are real transitions, not padding: a partial release
        # of an untouched reservation leaves it ACTIVE, and drawing more material
        # against an already part-issued one leaves it PARTIALLY_CONSUMED. Making
        # them explicit means every status write goes through assert_transition,
        # with no "unless it did not change" special case for a future edit to
        # get wrong.
        Transition(
            ReservationStatus.ACTIVE,
            ReservationStatus.ACTIVE,
            permission=Permission.RESERVATION_RELEASE,
            label="Release part",
        ),
        Transition(
            ReservationStatus.ACTIVE,
            ReservationStatus.PARTIALLY_CONSUMED,
            permission=Permission.RESERVATION_CREATE,
            label="Issue part",
            is_irreversible=True,
        ),
        Transition(
            ReservationStatus.ACTIVE,
            ReservationStatus.CONSUMED,
            permission=Permission.RESERVATION_CREATE,
            label="Issue in full",
            is_irreversible=True,
        ),
        Transition(
            ReservationStatus.ACTIVE,
            ReservationStatus.RELEASED,
            permission=Permission.RESERVATION_RELEASE,
            label="Release",
        ),
        Transition(
            ReservationStatus.ACTIVE,
            ReservationStatus.EXPIRED,
            label="Expire",
        ),
        Transition(
            ReservationStatus.PARTIALLY_CONSUMED,
            ReservationStatus.PARTIALLY_CONSUMED,
            permission=Permission.RESERVATION_CREATE,
            label="Issue more",
            is_irreversible=True,
        ),
        Transition(
            ReservationStatus.PARTIALLY_CONSUMED,
            ReservationStatus.CONSUMED,
            permission=Permission.RESERVATION_CREATE,
            label="Issue the remainder",
            is_irreversible=True,
        ),
        Transition(
            ReservationStatus.PARTIALLY_CONSUMED,
            ReservationStatus.RELEASED,
            permission=Permission.RESERVATION_RELEASE,
            label="Release the remainder",
        ),
        Transition(
            ReservationStatus.PARTIALLY_CONSUMED,
            ReservationStatus.EXPIRED,
            label="Expire the remainder",
        ),
        # There is deliberately no edge out of CONSUMED, RELEASED or EXPIRED.
        # Stock that has been issued cannot be un-issued by editing a promise;
        # the correction is a new movement, and the correction for a released
        # promise is a new reservation.
    ),
    terminal=frozenset(
        {ReservationStatus.CONSUMED, ReservationStatus.RELEASED, ReservationStatus.EXPIRED}
    ),
)

REALLOCATION_FLOW: StateMachine[ReallocationStatus] = StateMachine(
    entity="Reallocation request",
    initial=ReallocationStatus.PENDING,
    transitions=(
        Transition(
            ReallocationStatus.PENDING,
            ReallocationStatus.APPROVED,
            permission=Permission.ALLOCATION_APPROVE,
            label="Approve",
        ),
        Transition(
            ReallocationStatus.PENDING,
            ReallocationStatus.REJECTED,
            permission=Permission.ALLOCATION_APPROVE,
            label="Reject",
        ),
        # Approval and execution are one transaction but two edges: the audit
        # trail has to be able to show that the stock moved *after* somebody
        # signed for it, not merely at the same time.
        Transition(
            ReallocationStatus.APPROVED,
            ReallocationStatus.EXECUTED,
            permission=Permission.ALLOCATION_REALLOCATE,
            label="Execute",
            is_irreversible=True,
        ),
    ),
    terminal=frozenset({ReallocationStatus.REJECTED, ReallocationStatus.EXECUTED}),
)


# -----------------------------------------------------------------------------
# The configurable seam
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReallocationPolicy:
    """Ranking weights and eligibility thresholds for taking stock from a demand.

    This is the seam master rules section 13 requires: allocation priority is
    business-driven, so none of it is hardcoded in a comparison. Every field is
    read by :func:`rank_candidates`, which is pure - swap the policy and the
    change in behaviour is fully determined and unit-testable.

    The weights are on one scale so they can be compared. A candidate's score is
    a weighted sum of four signals, all bounded so no single one can dominate
    through an outlier: how much less urgent the donor is, how much later it
    needs the material, whether its promise is soft, and whether any of it has
    already been issued.
    """

    #: Points per unit of priority the donor is *less* urgent than the requester.
    #: The dominant term: urgency is the reason one demand may take from another.
    priority_weight: Decimal = Decimal("10")
    #: Points per day the donor needs the material *later* than the requester.
    #: Negative when the donor needs it sooner, which is a reason not to take it.
    required_date_weight: Decimal = Decimal("1")
    #: Flat bonus for a SOFT donor. Larger than a fortnight of date slack: a
    #: planning placeholder should lose to nothing, and giving it up disappoints
    #: nobody.
    soft_reservation_bonus: Decimal = Decimal("40")
    #: Flat bonus for a donor nothing has been issued against. Taking from a
    #: part-issued demand strands the material already on the shop floor.
    untouched_bonus: Decimal = Decimal("15")

    #: How much more urgent the requester must be before a donor is eligible at
    #: all. 1 means "strictly more urgent"; raise it to stop marginal priority
    #: differences from churning reservations.
    min_priority_gap: int = 1
    #: Bounds on the two graded signals, so one very old placeholder cannot
    #: outscore every genuine consideration.
    priority_gap_cap: int = 50
    date_slack_cap_days: int = 60

    #: A donor needed within this many days is protected and never suggested.
    #:
    #: ASSUMPTION: this stands in for "check the production start date" in master
    #: rules section 13. The production order's start date is not readable from
    #: this module - production owns that table, and reading it here would make
    #: allocation depend on a module it must not - so the reservation's own
    #: ``required_date``, which the demand document sets when it reserves, is the
    #: contract-visible proxy. Three days is the conservative choice: material
    #: for a job starting this week has usually already been kitted, and taking
    #: it back causes a second problem rather than solving the first.
    protect_within_days: int = 3

    #: Whether HARD reservations may be suggested at all. They still cannot be
    #: taken without an approved request; this switches off even offering them.
    include_hard_donors: bool = True
    #: Master rules section 13: a HARD reservation is never taken without an
    #: approved ReallocationRequest. A policy field rather than a bare constant
    #: so the rule is visible where the other allocation rules are - but see
    #: :func:`reallocate`, which asserts it regardless of the policy.
    hard_requires_approval: bool = True

    #: How many ranked candidates are returned to a screen.
    max_candidates: int = 10
    #: How many open reservations the database hands the ranking to consider.
    candidate_pool_limit: int = CANDIDATE_POOL_LIMIT

    def eligible_priority_ceiling(self, requester_priority: int) -> int:
        """The priority a donor must be strictly worse than to be eligible.

        Expressed as a ceiling so the whole eligibility rule can be pushed into
        the SQL ``WHERE`` clause instead of filtering rows in Python after
        fetching them.
        """
        return requester_priority + self.min_priority_gap - 1


#: The shipped ranking. Documented in ``docs/assumptions.md`` section 11; change
#: it there and here together, never by scattering a second interpretation.
DEFAULT_POLICY = ReallocationPolicy()


# -----------------------------------------------------------------------------
# Pure reservation arithmetic - unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrawdownPlan:
    """The effect of releasing or consuming part of a reservation.

    Computed before anything is written so the decision can be tested on its own,
    and so a refusal is a typed business error naming the numbers rather than an
    integrity error from a check constraint.
    """

    applied_quantity: Decimal
    remaining_before: Decimal
    remaining_after: Decimal
    next_status: ReservationStatus

    @property
    def closes_reservation(self) -> bool:
        return self.remaining_after == ZERO


def remaining_of(
    quantity: Decimal, consumed_quantity: Decimal, released_quantity: Decimal
) -> Decimal:
    """How much of a reservation is still standing.

    Computed in Python from the three stored columns rather than read from the
    generated ``remaining_quantity`` column, so it is correct immediately after
    an unflushed change - the release and consume paths both need the value
    before the row reaches the database.
    """
    return to_quantity(
        to_quantity(quantity) - to_quantity(consumed_quantity) - to_quantity(released_quantity)
    )


def _assert_open(status: ReservationStatus, verb: str, code: ErrorCode) -> None:
    if status not in OPEN_RESERVATION_STATUSES:
        raise BusinessRuleError(
            f"This reservation is {status} and cannot be {verb}.",
            code=code,
            details={"status": str(status)},
        )


def plan_release(
    *,
    quantity: Decimal,
    consumed_quantity: Decimal,
    released_quantity: Decimal,
    status: ReservationStatus,
    requested: Decimal | None = None,
) -> DrawdownPlan:
    """Work out a release. ``requested`` omitted means "whatever is left".

    Releasing more than remains raises rather than clamping: a caller asking to
    give back 50 when 30 is outstanding has miscounted, and silently releasing 30
    would hide the mistake behind a successful response.
    """
    _assert_open(status, "released", ErrorCode.BUSINESS_RULE_VIOLATION)
    remaining = remaining_of(quantity, consumed_quantity, released_quantity)

    applied = remaining if requested is None else to_quantity(requested)
    if applied <= ZERO:
        raise BusinessRuleError(
            "A release must be for a positive quantity.",
            details={"requested": str(applied)},
        )
    if applied > remaining:
        raise BusinessRuleError(
            f"Cannot release {applied}: only {remaining} of this reservation is outstanding.",
            details={"requested": str(applied), "remaining": str(remaining)},
        )

    remaining_after = to_quantity(remaining - applied)
    # A partial release leaves the reservation where it was - still ACTIVE, or
    # still PARTIALLY_CONSUMED if material has already been drawn against it.
    next_status = ReservationStatus.RELEASED if remaining_after == ZERO else status
    return DrawdownPlan(
        applied_quantity=applied,
        remaining_before=remaining,
        remaining_after=remaining_after,
        next_status=next_status,
    )


def plan_consume(
    *,
    quantity: Decimal,
    consumed_quantity: Decimal,
    released_quantity: Decimal,
    status: ReservationStatus,
    requested: Decimal,
) -> DrawdownPlan:
    """Work out a consumption: how much becomes a real movement, and what status.

    Note the asymmetry with :func:`plan_release`: a fully drawn reservation is
    ``CONSUMED`` even when part of it was released along the way, because the
    last thing that happened to it was an issue. The individual events remain in
    the audit log; the status only has to say whether anything is outstanding.
    """
    _assert_open(status, "consumed", ErrorCode.RESERVATION_ALREADY_CONSUMED)
    remaining = remaining_of(quantity, consumed_quantity, released_quantity)

    applied = to_quantity(requested)
    if applied <= ZERO:
        raise BusinessRuleError(
            "A consumption must be for a positive quantity.",
            details={"requested": str(applied)},
        )
    if applied > remaining:
        raise BusinessRuleError(
            f"Cannot consume {applied}: only {remaining} of this reservation is outstanding.",
            code=ErrorCode.RESERVATION_ALREADY_CONSUMED,
            details={"requested": str(applied), "remaining": str(remaining)},
        )

    remaining_after = to_quantity(remaining - applied)
    next_status = (
        ReservationStatus.CONSUMED
        if remaining_after == ZERO
        else ReservationStatus.PARTIALLY_CONSUMED
    )
    return DrawdownPlan(
        applied_quantity=applied,
        remaining_before=remaining,
        remaining_after=remaining_after,
        next_status=next_status,
    )


# -----------------------------------------------------------------------------
# Pure reallocation ranking - unit tested without a database
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReallocationContext:
    """The demand asking for stock somebody else is holding."""

    demand_type: DemandType
    demand_id: uuid.UUID
    quantity_needed: Decimal
    priority: int = DEFAULT_RESERVATION_PRIORITY
    required_date: date | None = None


@dataclass(frozen=True, slots=True)
class ReallocationCandidate:
    """A reservation the request could take from, with its rank and the reasons.

    ``reasons`` exists because the output of this engine is a request to
    disappoint somebody. An approver who cannot see why a donor was chosen has to
    either rubber-stamp it or redo the analysis by hand, and both are worse than
    the engine explaining itself.
    """

    reservation_id: uuid.UUID
    item_id: uuid.UUID
    warehouse_id: uuid.UUID
    location_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    demand_type: DemandType
    demand_id: uuid.UUID
    demand_code: str | None
    reservation_type: ReservationType
    priority: int
    required_date: date | None
    quantity: Decimal
    consumed_quantity: Decimal
    remaining_quantity: Decimal
    takeable_quantity: Decimal
    requires_approval: bool
    score: Decimal
    reasons: tuple[str, ...]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def candidate_eligibility(
    facts: ReservationFacts,
    *,
    request: ReallocationContext,
    policy: ReallocationPolicy,
    as_of: date,
) -> str | None:
    """Why this reservation cannot be a donor, or ``None`` if it can be.

    Returns the reason rather than a bool so the caller can tell a planner *why*
    the obvious candidate was not offered - "needed in two days", "already being
    reallocated" - instead of showing an empty list.
    """
    if facts.status not in OPEN_RESERVATION_STATUSES:
        return f"reservation is {facts.status}"
    if facts.remaining_quantity <= ZERO:
        return "nothing outstanding to take"
    if facts.demand_type == request.demand_type and facts.demand_id == request.demand_id:
        return "belongs to the requesting demand"
    if facts.priority - request.priority < policy.min_priority_gap:
        return (
            f"donor priority {facts.priority} is not at least "
            f"{policy.min_priority_gap} less urgent than {request.priority}"
        )
    if facts.reservation_type is ReservationType.HARD and not policy.include_hard_donors:
        return "hard reservations are excluded by the active policy"
    if facts.unclaimed_quantity <= ZERO:
        return "already claimed by another reallocation request"
    if facts.required_date is not None:
        days_away = (facts.required_date - as_of).days
        if days_away <= policy.protect_within_days:
            return (
                f"needed in {days_away} day(s), inside the "
                f"{policy.protect_within_days}-day protection window"
            )
    return None


def score_candidate(
    facts: ReservationFacts,
    *,
    request: ReallocationContext,
    policy: ReallocationPolicy,
) -> tuple[Decimal, tuple[str, ...]]:
    """Score one eligible donor and explain the score.

    Higher is a better donor. Every term is bounded by a policy cap, so a
    reservation with a priority of 9999 or a required date three years out cannot
    swamp the other considerations.
    """
    reasons: list[str] = []
    score = ZERO

    gap = _clamp(facts.priority - request.priority, 0, policy.priority_gap_cap)
    if gap:
        score += policy.priority_weight * gap
        reasons.append(f"{gap} priority level(s) less urgent than the requesting demand")

    if facts.required_date is not None and request.required_date is not None:
        slack = _clamp(
            (facts.required_date - request.required_date).days,
            -policy.date_slack_cap_days,
            policy.date_slack_cap_days,
        )
        if slack:
            score += policy.required_date_weight * slack
            reasons.append(
                f"needed {slack} day(s) later than the requesting demand"
                if slack > 0
                else f"needed {abs(slack)} day(s) sooner than the requesting demand"
            )
    else:
        reasons.append("no comparable required date")

    if facts.reservation_type is ReservationType.SOFT:
        score += policy.soft_reservation_bonus
        reasons.append("soft reservation - a planning placeholder, not a commitment")
    else:
        reasons.append("hard reservation - needs an approved request to take")

    if facts.consumed_quantity <= ZERO:
        score += policy.untouched_bonus
        reasons.append("no material issued against it yet")
    else:
        reasons.append(f"{facts.consumed_quantity} already issued against it")

    return score, tuple(reasons)


def rank_candidates(
    pool: list[ReservationFacts],
    *,
    request: ReallocationContext,
    policy: ReallocationPolicy = DEFAULT_POLICY,
    as_of: date | None = None,
    prefer_reservation_id: uuid.UUID | None = None,
) -> list[ReallocationCandidate]:
    """Rank the donors that could cover ``request.quantity_needed``.

    Pure: everything it needs has already been fetched, so the ranking can be
    exercised without a database and no policy rule can grow a query.

    The returned list is cumulative - each candidate's ``takeable_quantity`` is
    what it would contribute *after* the ones above it, and the list stops once
    the shortfall is covered. A screen can therefore show the whole answer, not
    a menu the user has to add up.

    ``prefer_reservation_id`` names the donor a planner has already chosen. It
    is promoted to the head of the ranking *after* eligibility has been decided,
    never through it: a reservation that is too urgent, protected or already
    claimed stays out of the list however explicitly it was asked for. The
    promotion is needed because the list is cumulative and truncated - without
    it a perfectly eligible donor is dropped merely because a better-scoring one
    already covered the shortfall, and the caller is then told, wrongly, that it
    was not eligible.
    """
    moment = as_of or today()
    needed = to_quantity(request.quantity_needed)
    if needed <= ZERO:
        return []

    scored: list[tuple[Decimal, ReservationFacts, tuple[str, ...]]] = []
    for facts in pool:
        if candidate_eligibility(facts, request=request, policy=policy, as_of=moment) is not None:
            continue
        score, reasons = score_candidate(facts, request=request, policy=policy)
        scored.append((score, facts, reasons))

    # Deterministic ordering. Score first, then the individual signals in the
    # order the business states them, then the id - so two runs over the same
    # data always produce the same list and an approval screen does not reshuffle
    # under the approver. The chosen donor, when there is one, sorts ahead of
    # everything else - it is the one the request will actually be raised
    # against, so a screen that showed it buried under alternatives would be
    # describing a different decision.
    scored.sort(
        key=lambda entry: (
            0 if entry[1].id == prefer_reservation_id else 1,
            -entry[0],
            -entry[1].priority,
            0 if entry[1].reservation_type is ReservationType.SOFT else 1,
            -entry[1].unclaimed_quantity,
            entry[1].id.bytes,
        )
    )

    candidates: list[ReallocationCandidate] = []
    outstanding = needed
    for score, facts, reasons in scored:
        if outstanding <= ZERO or len(candidates) >= policy.max_candidates:
            break
        takeable = min(facts.unclaimed_quantity, outstanding)
        if takeable <= ZERO:
            continue
        outstanding = to_quantity(outstanding - takeable)
        candidates.append(
            ReallocationCandidate(
                reservation_id=facts.id,
                item_id=facts.item_id,
                warehouse_id=facts.warehouse_id,
                location_id=facts.location_id,
                batch_id=facts.batch_id,
                demand_type=facts.demand_type,
                demand_id=facts.demand_id,
                demand_code=facts.demand_code,
                reservation_type=facts.reservation_type,
                priority=facts.priority,
                required_date=facts.required_date,
                quantity=to_quantity(facts.quantity),
                consumed_quantity=to_quantity(facts.consumed_quantity),
                remaining_quantity=to_quantity(facts.remaining_quantity),
                takeable_quantity=to_quantity(takeable),
                requires_approval=(
                    facts.reservation_type is ReservationType.HARD and policy.hard_requires_approval
                ),
                score=score,
                reasons=reasons,
            )
        )
    return candidates


# -----------------------------------------------------------------------------
# Demand type to movement type
# -----------------------------------------------------------------------------

#: Which movement a consumed reservation becomes. The reservation knows what the
#: stock was promised to, so the caller does not have to restate it and cannot
#: get it wrong - a dispatch posted as a material issue would put a customer
#: delivery in the production consumption report.
#:
#: ASSUMPTION: a SPARE reservation is consumed as a MATERIAL_ISSUE. A spare part
#: leaves stock either as a service issue or as a sale, and the movement
#: vocabulary in section 83 has no spare-specific member; MATERIAL_ISSUE is the
#: conservative reading because it does not claim a customer dispatch happened.
#: A caller that knows better passes ``transaction_type`` explicitly.
CONSUMPTION_MOVEMENT: dict[DemandType, TransactionType] = {
    DemandType.SALES_ORDER: TransactionType.DISPATCH,
    DemandType.PRODUCTION_ORDER: TransactionType.MATERIAL_ISSUE,
    DemandType.ASSEMBLY_ORDER: TransactionType.ASSEMBLY_CONSUME,
    DemandType.JOB_WORK_ORDER: TransactionType.JOB_WORK_ISSUE,
    DemandType.SPARE: TransactionType.MATERIAL_ISSUE,
    # Only the outbound leg. The receiving leg belongs to the transfer document,
    # which posts TRANSFER_IN when the goods actually arrive.
    DemandType.TRANSFER: TransactionType.TRANSFER_OUT,
}


# -----------------------------------------------------------------------------
# The engine
# -----------------------------------------------------------------------------


class ReservationEngine:
    """Locking, validation and persistence for reservations.

    Everything that changes ``reserved_quantity`` goes through
    :meth:`apply_reserve` or :meth:`apply_drawdown`, both of which require an
    already-locked balance row. That is what lets :func:`reallocate` perform a
    release and a reserve against one lock, with the net effect on the balance
    being exactly zero - which is the whole reason a reallocation cannot lose or
    invent stock.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.reservations = StockReservationRepository(session, tenant)
        self.requests = ReallocationRequestRepository(session, tenant)
        self.inventory = InventoryRepository(session, tenant)
        # The movement engine is used for its lock, not for posting: this is the
        # identical primitive post_movement takes, which is what makes a
        # reservation and an issue contend for the same row.
        self.movements = MovementEngine(session, tenant)

    # --- locking ------------------------------------------------------------

    async def lock_balance(self, grain: BalanceGrain) -> InventoryBalance:
        """Lock one stock position and re-read it.

        Delegates to the inventory engine so the ``lock_timeout``, the
        create-if-missing upsert and the primary-key ordering are the same ones
        every stock movement uses. A second implementation here would be a second
        thing to keep in step.
        """
        locked = await self.movements.lock([grain])
        balance = locked.get(grain)
        if balance is None:  # pragma: no cover - lock_balances creates every grain
            raise BusinessRuleError("The stock position could not be locked for update.")
        return balance

    async def lock_reservation(self, reservation_id: uuid.UUID) -> StockReservation:
        """Lock one reservation and re-read it, or raise a clean not-found.

        The caller has already read the row unlocked to learn its position, so a
        miss here means it disappeared in between. That should be impossible -
        reservations are never deleted - but a KeyError escaping as a 500 would
        tell an operator nothing, and the tenant-scoped miss is indistinguishable
        from "belongs to another company", which is the answer we want to give
        anyway.
        """
        locked = await self.reservations.lock_reservations([reservation_id])
        reservation = locked.get(reservation_id)
        if reservation is None:
            raise NotFoundError("Stock reservation", reservation_id)
        return reservation

    # --- validation ---------------------------------------------------------

    async def resolve_position(
        self, item_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> tuple[ItemFacts, str]:
        """Confirm the item and warehouse exist, and return their labels.

        Done *before* the balance lock: the create-if-missing upsert would
        otherwise turn an unknown item id into a foreign-key violation rather
        than a clean 404, and an integrity error tells the caller nothing.
        """
        items = await self.inventory.item_facts([item_id])
        item = items.get(item_id)
        if item is None:
            raise NotFoundError("Item", item_id)
        warehouses = await self.inventory.warehouse_facts([warehouse_id])
        warehouse = warehouses.get(warehouse_id)
        if warehouse is None:
            raise NotFoundError("Warehouse", warehouse_id)
        return item, warehouse.code

    # --- balance effects ----------------------------------------------------

    @staticmethod
    def promisable(balance: InventoryBalance) -> Decimal:
        """What is still promisable at this position, computed in Python.

        Deliberately *not* read from ``available_quantity``. That column is
        generated by PostgreSQL, so its value tracks the row as the database last
        saw it - and a reallocation lowers ``reserved_quantity`` and raises it
        again before anything is flushed. Reading the stored column between those
        two steps would see the pre-release figure and refuse a reallocation that
        is exactly balanced. Recomputing the same formula from the columns this
        code controls is correct at every point in the transaction.
        """
        return to_quantity(
            to_quantity(balance.on_hand_quantity)
            - to_quantity(balance.reserved_quantity)
            - to_quantity(balance.qc_hold_quantity)
            - to_quantity(balance.damaged_quantity)
        )

    def apply_reserve(
        self,
        balance: InventoryBalance,
        quantity: Decimal,
        *,
        item: ItemFacts,
        warehouse_code: str,
    ) -> Decimal:
        """Raise ``reserved_quantity`` on an already-locked balance row.

        Returns what was promisable *before* the change, which is what the audit
        entry records - after the change it is derivable, before it is the number
        the decision was made against.

        The check is deliberately stricter than the database's. The constraint
        floor is ``reserved <= on_hand``; this refuses unless the quantity fits
        inside what is promisable, which additionally excludes stock that is on QC
        hold or damaged. Promising goods that are physically present but not
        usable is how a delivery date is missed on the day of dispatch.

        ``allow_negative_stock`` is not consulted. A warehouse may be configured
        to let line-side WIP go negative on an issue; it may never be configured
        to let the company promise stock it does not have.
        """
        available = self.promisable(balance)
        if quantity > available:
            insufficient_stock_events.inc()
            raise InsufficientStockError(
                item_code=item.code,
                warehouse_code=warehouse_code,
                requested=quantity,
                available=available,
            )

        balance.reserved_quantity = to_quantity(to_quantity(balance.reserved_quantity) + quantity)
        balance.updated_by_id = persisted_actor(self.tenant.user_id)
        return available

    def apply_drawdown(self, balance: InventoryBalance, quantity: Decimal) -> None:
        """Lower ``reserved_quantity`` on an already-locked balance row.

        The guard is not defensive clutter: if the balance ever held less than a
        reservation claims, letting the subtraction run would trip the
        ``reserved_quantity >= 0`` constraint and surface as an opaque integrity
        error at flush time, with no indication of which reservation was wrong.
        """
        reserved = to_quantity(balance.reserved_quantity)
        if quantity > reserved:
            raise BusinessRuleError(
                "The stock position holds less reserved stock than this reservation claims; "
                "reservations and balances are out of step and must be reconciled.",
                details={
                    "balance_reserved": str(reserved),
                    "requested": str(quantity),
                    "balance_id": str(balance.id),
                },
            )
        balance.reserved_quantity = to_quantity(reserved - quantity)
        balance.updated_by_id = persisted_actor(self.tenant.user_id)

    # --- audit --------------------------------------------------------------

    async def audit_reservation(
        self,
        reservation: StockReservation,
        *,
        summary: str,
        action: AuditAction = AuditAction.RESERVATION_CHANGE,
        context: dict[str, Any] | None = None,
    ) -> None:
        await record_audit(
            self.session,
            company_id=self.tenant.company_id,
            action=action,
            entity_type=AUDIT_ENTITY_RESERVATION,
            entity_id=reservation.id,
            # The reservation has no code of its own; the demand's document
            # number is what a person searching the audit log actually types.
            entity_code=reservation.demand_code,
            summary=summary,
            user_id=self.tenant.user_id,
            context={
                "item_id": reservation.item_id,
                "warehouse_id": reservation.warehouse_id,
                "demand_type": str(reservation.demand_type),
                "demand_id": reservation.demand_id,
                "demand_line_id": reservation.demand_line_id,
                "reservation_type": str(reservation.reservation_type),
                "quantity": reservation.quantity,
                "consumed_quantity": reservation.consumed_quantity,
                "released_quantity": reservation.released_quantity,
                "status": str(reservation.status),
                "priority": reservation.priority,
                **(context or {}),
            },
        )


# -----------------------------------------------------------------------------
# Public service API - database contract section 4.5
# -----------------------------------------------------------------------------


async def reserve(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: Decimal,
    demand_type: DemandType,
    demand_id: uuid.UUID,
    reason: str,
    demand_line_id: uuid.UUID | None = None,
    demand_code: str | None = None,
    reservation_type: ReservationType = ReservationType.HARD,
    priority: int = DEFAULT_RESERVATION_PRIORITY,
    required_date: date | None = None,
    expires_at: datetime | None = None,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> StockReservation:
    """Hold a quantity of stock for one demand line.

    Takes the same ``inventory_balances`` row lock ``post_movement`` takes,
    re-reads the quantities after acquiring it, and refuses with
    :class:`~src.core.errors.InsufficientStockError` if the reservation would
    push ``reserved_quantity`` past what is actually promisable. Then it raises
    ``reserved_quantity``, writes the reservation row and writes a
    ``RESERVATION_CHANGE`` audit entry - all in one transaction.

    No ``inventory_transaction`` is written. Nothing has moved: the goods are
    still on the shelf, still owned, still countable. Only the promise changed.

    The reservation is pinned to a stock *position*, not to a warehouse total.
    Reserving without a location or batch holds the position that has neither,
    which is the same position an issue posted without them will draw from - so
    a reservation and its eventual consumption always meet on one balance row.
    """
    engine = ReservationEngine(session, tenant)
    amount = to_quantity(quantity)
    if amount <= ZERO:
        raise BusinessRuleError(
            "A reservation must be for a positive quantity.", details={"requested": str(amount)}
        )

    async with transaction(session):
        item, warehouse_code = await engine.resolve_position(item_id, warehouse_id)
        grain = BalanceGrain(
            item_id=item_id,
            warehouse_id=warehouse_id,
            location_id=location_id,
            batch_id=batch_id,
        )
        balance = await engine.lock_balance(grain)
        promisable_before = engine.apply_reserve(
            balance, amount, item=item, warehouse_code=warehouse_code
        )

        reservation = StockReservation(
            item_id=item_id,
            warehouse_id=warehouse_id,
            location_id=location_id,
            batch_id=batch_id,
            demand_type=demand_type,
            demand_id=demand_id,
            demand_line_id=demand_line_id,
            demand_code=demand_code,
            reservation_type=reservation_type,
            quantity=amount,
            consumed_quantity=ZERO,
            released_quantity=ZERO,
            status=ReservationStatus.ACTIVE,
            priority=priority,
            required_date=required_date,
            expires_at=expires_at,
            reason=reason,
        )
        engine.reservations.add(reservation)
        await session.flush()

        await engine.audit_reservation(
            reservation,
            summary=(
                f"Reserved {amount} {item.code} at {warehouse_code} for "
                f"{demand_type} {demand_code or demand_id} ({reason})"
            ),
            context={
                "item_code": item.code,
                "warehouse_code": warehouse_code,
                "balance_reserved_after": balance.reserved_quantity,
                "promisable_before": promisable_before,
                "promisable_after": to_quantity(promisable_before - amount),
                "reason": reason,
            },
        )
    return reservation


async def release(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    reservation_id: uuid.UUID,
    reason: str,
    quantity: Decimal | None = None,
) -> StockReservation:
    """Give stock back to general availability, in part or in full.

    ``quantity`` omitted releases everything still outstanding - what cancelling
    a demand document does. Releasing more than remains raises
    :class:`~src.core.errors.BusinessRuleError` rather than clamping, because a
    caller that has miscounted needs to find out.

    The reason is mandatory and is written to the row and to the audit entry.
    Section 83 requires a reason for every stock event, and "released" is not one.
    """
    engine = ReservationEngine(session, tenant)

    async with transaction(session):
        # Read unlocked first, only to learn which balance row to lock. The
        # position of a reservation never changes, so this value cannot go stale
        # in a way that matters; every quantity is re-read under the locks below.
        preview = await engine.reservations.get(reservation_id)
        if preview is None:
            raise NotFoundError("Stock reservation", reservation_id)
        grain = BalanceGrain(
            item_id=preview.item_id,
            warehouse_id=preview.warehouse_id,
            location_id=preview.location_id,
            batch_id=preview.batch_id,
        )
        # Balance first, then the reservation. That order is fixed across this
        # module so a release and a reallocation cannot deadlock each other.
        balance = await engine.lock_balance(grain)
        reservation = await engine.lock_reservation(reservation_id)

        plan = plan_release(
            quantity=reservation.quantity,
            consumed_quantity=reservation.consumed_quantity,
            released_quantity=reservation.released_quantity,
            status=reservation.status,
            requested=quantity,
        )
        previous = reservation.status
        RESERVATION_FLOW.assert_transition(previous, plan.next_status)

        engine.apply_drawdown(balance, plan.applied_quantity)
        reservation.released_quantity = to_quantity(
            to_quantity(reservation.released_quantity) + plan.applied_quantity
        )
        reservation.status = plan.next_status
        # NULL when the release is a scheduled top-up re-reserving the line; a
        # person releasing it is recorded as themselves.
        reservation.released_by_id = persisted_actor(tenant.user_id)
        reservation.released_at = utcnow()
        # The row keeps the most recent reason so a screen can show it without a
        # second query; the audit log keeps every individual release.
        reservation.release_reason = reason[:255]
        engine.reservations.touch(reservation)
        await session.flush()

        await engine.audit_reservation(
            reservation,
            summary=(
                f"Released {plan.applied_quantity} of reservation for "
                f"{reservation.demand_type} {reservation.demand_code or reservation.demand_id} "
                f"({reason})"
            ),
            context={
                "released_now": plan.applied_quantity,
                "remaining_before": plan.remaining_before,
                "remaining_after": plan.remaining_after,
                "balance_reserved_after": balance.reserved_quantity,
                "release_reason": reason,
            },
        )
        if plan.next_status is not previous:
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_ENTITY_RESERVATION,
                entity_id=reservation.id,
                entity_code=reservation.demand_code,
                from_status=str(previous),
                to_status=str(plan.next_status),
                user_id=tenant.user_id,
                action=AuditAction.RESERVATION_CHANGE,
                reason=reason,
            )
    return reservation


async def consume(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    reservation_id: uuid.UUID,
    quantity: Decimal,
    reference_type: str,
    reference_id: uuid.UUID | None,
    reference_code: str | None,
    transaction_type: TransactionType | None = None,
    occurred_at: datetime | None = None,
    serial_ids: Sequence[uuid.UUID] = (),
) -> StockReservation:
    """Turn part or all of a reservation into a real stock movement.

    The order of the two effects is the correctness argument, not a style
    preference. ``reserved_quantity`` is lowered first and flushed, *then*
    :func:`~src.modules.inventory.service.post_movement` posts the issue or
    dispatch. Posting first would present the movement engine with a balance
    where ``reserved`` exceeds the on-hand it is about to reduce, and it would
    correctly refuse. Both happen in one transaction, so ``reserved_quantity``
    and ``on_hand_quantity`` cannot end up disagreeing.

    The flush before the posting is also load-bearing: the movement engine
    re-reads the balance row with ``populate_existing``, which would overwrite an
    unflushed in-memory change with the value still in the database.

    ``serial_ids`` names the units of a serial-tracked item, one per unit. Such
    an item cannot leave as an anonymous quantity (docs/assumptions.md section
    23), so the drawdown is posted as one movement of one unit per serial - the
    same shape a direct dispatch of the item posts. A reservation never pins a
    serial (``serial_id`` is not part of the balance grain), so the units drawn
    are whichever the caller names, from the reservation's own position.
    """
    engine = ReservationEngine(session, tenant)
    units = list(serial_ids)
    if units and (
        len(set(units)) != len(units) or to_quantity(quantity) != to_quantity(Decimal(len(units)))
    ):
        raise BusinessRuleError(
            f"{len(units)} serial(s) were named for a consumption of {to_quantity(quantity)}; "
            "a serial-tracked drawdown names exactly one distinct unit per unit consumed.",
            code=ErrorCode.SERIAL_REQUIRED,
            details={"requested": str(to_quantity(quantity)), "serials": len(units)},
        )

    async with transaction(session):
        preview = await engine.reservations.get(reservation_id)
        if preview is None:
            raise NotFoundError("Stock reservation", reservation_id)
        grain = BalanceGrain(
            item_id=preview.item_id,
            warehouse_id=preview.warehouse_id,
            location_id=preview.location_id,
            batch_id=preview.batch_id,
        )
        balance = await engine.lock_balance(grain)
        reservation = await engine.lock_reservation(reservation_id)

        plan = plan_consume(
            quantity=reservation.quantity,
            consumed_quantity=reservation.consumed_quantity,
            released_quantity=reservation.released_quantity,
            status=reservation.status,
            requested=quantity,
        )
        previous = reservation.status
        RESERVATION_FLOW.assert_transition(previous, plan.next_status)

        engine.apply_drawdown(balance, plan.applied_quantity)
        reservation.consumed_quantity = to_quantity(
            to_quantity(reservation.consumed_quantity) + plan.applied_quantity
        )
        reservation.status = plan.next_status
        engine.reservations.touch(reservation)
        # Must precede post_movement - see the docstring.
        await session.flush()

        movement_type = transaction_type or CONSUMPTION_MOVEMENT[reservation.demand_type]
        movement_reason = (
            f"{movement_type} against reservation for {reservation.demand_type} "
            f"{reservation.demand_code or reservation.demand_id}"
        )[:255]
        if units:
            posted = await post_movements(
                session,
                tenant,
                [
                    MovementRequest(
                        item_id=reservation.item_id,
                        warehouse_id=reservation.warehouse_id,
                        quantity=Decimal("1"),
                        transaction_type=movement_type,
                        reference_type=reference_type,
                        reference_id=reference_id,
                        reference_code=reference_code,
                        reason=movement_reason,
                        location_id=reservation.location_id,
                        batch_id=reservation.batch_id,
                        serial_id=serial_id,
                        occurred_at=occurred_at,
                    )
                    for serial_id in units
                ],
            )
        else:
            posted = [
                await post_movement(
                    session,
                    tenant,
                    item_id=reservation.item_id,
                    warehouse_id=reservation.warehouse_id,
                    quantity=plan.applied_quantity,
                    transaction_type=movement_type,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    reference_code=reference_code,
                    reason=movement_reason,
                    location_id=reservation.location_id,
                    batch_id=reservation.batch_id,
                    occurred_at=occurred_at,
                )
            ]

        await engine.audit_reservation(
            reservation,
            summary=(
                f"Consumed {plan.applied_quantity} of reservation for "
                f"{reservation.demand_type} {reservation.demand_code or reservation.demand_id} "
                f"as {movement_type} {reference_code or ''}".strip()
            ),
            context={
                "consumed_now": plan.applied_quantity,
                "remaining_after": plan.remaining_after,
                "transaction_type": str(movement_type),
                "inventory_transaction_id": posted[0].id,
                "inventory_transaction_ids": [row.id for row in posted] if units else None,
                "reference_type": reference_type,
                "reference_id": reference_id,
                "reference_code": reference_code,
                "balance_reserved_after": balance.reserved_quantity,
            },
        )
        await record_status_change(
            session,
            company_id=tenant.company_id,
            entity_type=AUDIT_ENTITY_RESERVATION,
            entity_id=reservation.id,
            entity_code=reservation.demand_code,
            from_status=str(previous),
            to_status=str(plan.next_status),
            user_id=tenant.user_id,
            action=AuditAction.RESERVATION_CHANGE,
            reason=f"{movement_type} {reference_code or ''}".strip(),
        )
    return reservation


async def available_to_promise(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID | None = None,
) -> Decimal:
    """Sum of ``available_quantity`` for one item.

    **Never cached** - master rules section 34, restated in
    ``docs/performance.md``. A cached answer that is even slightly stale is how
    two customers get promised the same stock, and the read is a single indexed
    sum over a narrow table. There is no Redis path here, deliberately, and one
    must not be added.

    Sums across every position of the item, which is a *planning* number: it can
    legitimately exceed what any single :func:`reserve` call will accept, because
    a reservation is pinned to one position while this is the whole warehouse (or
    the whole company when ``warehouse_id`` is omitted).
    """
    return await inventory_available(session, tenant, item_id, warehouse_id)


async def suggest_reallocation(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity_needed: Decimal,
    for_demand_type: DemandType,
    for_demand_id: uuid.UUID,
    required_date: date | None = None,
    priority: int = DEFAULT_RESERVATION_PRIORITY,
    policy: ReallocationPolicy = DEFAULT_POLICY,
    prefer_reservation_id: uuid.UUID | None = None,
) -> list[ReallocationCandidate]:
    """Which reservations could give this demand the stock it is short of.

    Suggests only. Nothing here changes a reservation or a balance: a HARD
    reservation is never taken without an approved
    :class:`~src.modules.allocation.models.ReallocationRequest`, and even a SOFT
    one goes through the request so the decision is recorded.

    The four checks master rules section 13 asks for, in order:

    1. **Free stock first.** What is already available is netted off before any
       reallocation is considered - taking stock from another demand when there
       is unreserved stock on the shelf would be gratuitous.
    2. **Current reservations.** The candidate pool is the open reservations at
       this position, read from the database rather than assumed.
    3. **Priority.** A donor must be at least ``policy.min_priority_gap`` less
       urgent than the requester; the comparison is in the SQL so ineligible rows
       are never fetched.
    4. **Material already issued, and how soon it is needed.** A part-consumed
       donor loses its untouched bonus, and one needed inside the protection
       window is not offered at all - see
       :attr:`ReallocationPolicy.protect_within_days` for why that window stands
       in for the production start date.
    """
    engine = ReservationEngine(session, tenant)
    needed = to_quantity(quantity_needed)
    if needed <= ZERO:
        return []

    free = await available_to_promise(session, tenant, item_id=item_id, warehouse_id=warehouse_id)
    shortfall = to_quantity(needed - max(ZERO, to_quantity(free)))
    if shortfall <= ZERO:
        return []

    pool = await engine.reservations.candidate_pool(
        item_id,
        warehouse_id,
        worse_than_priority=policy.eligible_priority_ceiling(priority),
        exclude_demand_type=for_demand_type,
        exclude_demand_id=for_demand_id,
        limit=policy.candidate_pool_limit,
    )
    if not pool:
        return []

    # One grouped query for the whole pool: a donor already promised to another
    # pending request must not be offered a second time.
    claims = await engine.requests.claimed_quantities([facts.id for facts in pool])
    pool = [facts.with_claim(claims.get(facts.id, ZERO)) for facts in pool]

    return rank_candidates(
        pool,
        request=ReallocationContext(
            demand_type=for_demand_type,
            demand_id=for_demand_id,
            quantity_needed=shortfall,
            priority=priority,
            required_date=required_date,
        ),
        policy=policy,
        prefer_reservation_id=prefer_reservation_id,
    )


async def reallocate(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    request_id: uuid.UUID,
    approved_by: uuid.UUID,
    decision_note: str | None = None,
) -> ReallocationRequest:
    """Approve a reallocation request and execute it, in one transaction.

    Executing means: release the quantity from the donor reservation, then
    reserve it for the recipient demand - both against the same locked balance
    row, so the net effect on ``reserved_quantity`` is exactly zero and no stock
    can be lost or invented in between. Master rules section 13 forbids
    implementing this as a direct stock update, and this is why: expressed as a
    release plus a reserve, every consequence is visible in the reservation
    table and the audit log, and the donor's demand can be told what happened.

    Both state-machine edges are asserted - ``PENDING -> APPROVED`` and
    ``APPROVED -> EXECUTED`` - because an audit trail that cannot show the
    approval preceding the effect is not an approval trail.

    Two people are notified. The donor's owner is told their order just lost
    material it was promised - finding that out from a shortage report next week
    is not acceptable - and the requester is told their request was approved, so
    they do not have to keep polling the approval inbox.
    """
    engine = ReservationEngine(session, tenant)

    async with transaction(session):
        request = await engine.requests.lock_request(request_id)
        if request is None:
            raise NotFoundError("Reallocation request", request_id)

        REALLOCATION_FLOW.assert_transition(request.status, ReallocationStatus.APPROVED)
        _assert_separate_approver(request, tenant, approved_by)

        donor_preview = await engine.reservations.get(request.from_reservation_id)
        if donor_preview is None:
            raise NotFoundError("Stock reservation", request.from_reservation_id)
        # The request copies the item and warehouse from the donor when it is
        # raised, so these always agree. Checked anyway: the execution locks the
        # donor's position but validates against the request's, and a row that
        # disagreed would reserve at one position what it released at another.
        if (
            donor_preview.item_id != request.item_id
            or donor_preview.warehouse_id != request.warehouse_id
        ):
            raise BusinessRuleError(
                "The reallocation request and its donor reservation describe "
                "different stock positions.",
                details={"request_id": str(request.id), "reservation_id": str(donor_preview.id)},
            )

        item, warehouse_code = await engine.resolve_position(request.item_id, request.warehouse_id)
        # Donor and recipient share one position - the recipient is being given
        # the donor's stock, not stock in general - so one balance row, one lock,
        # and a release and a reserve that provably cancel out.
        grain = BalanceGrain(
            item_id=donor_preview.item_id,
            warehouse_id=donor_preview.warehouse_id,
            location_id=donor_preview.location_id,
            batch_id=donor_preview.batch_id,
        )
        sales_targets: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = []
        donor_target_key: tuple[uuid.UUID, uuid.UUID] | None = None
        recipient_target_key: tuple[uuid.UUID, uuid.UUID] | None = None
        if donor_preview.demand_type is DemandType.SALES_ORDER:
            if donor_preview.demand_line_id is None:
                raise BusinessRuleError(
                    "A sales-order donor reservation must identify its demand line.",
                    details={"reservation_id": str(donor_preview.id)},
                )
            donor_target_key = (donor_preview.demand_id, donor_preview.demand_line_id)
            sales_targets.append(
                (
                    donor_preview.demand_id,
                    donor_preview.demand_line_id,
                    donor_preview.item_id,
                    donor_preview.warehouse_id,
                )
            )
        if request.to_demand_type is DemandType.SALES_ORDER:
            demand_line_id = _recipient_demand_line_id(request)
            if demand_line_id is None:
                raise BusinessRuleError(
                    "A sales-order reallocation must identify its demand line.",
                    details={"request_id": str(request.id)},
                )
            recipient_target_key = (request.to_demand_id, demand_line_id)
            sales_targets.append(
                (
                    request.to_demand_id,
                    demand_line_id,
                    request.item_id,
                    request.warehouse_id,
                )
            )
        donor_target = None
        recipient_target = None
        if sales_targets:
            from src.modules.sales.service import lock_reallocation_targets

            targets = await lock_reallocation_targets(session, tenant, targets=sales_targets)
            donor_target = targets.get(donor_target_key) if donor_target_key else None
            recipient_target = targets.get(recipient_target_key) if recipient_target_key else None
        balance = await engine.lock_balance(grain)
        recipient_line = recipient_target.line if recipient_target is not None else None

        reservation_ids = [request.from_reservation_id]
        if donor_target is not None:
            donor_reservation_id = donor_target.line.reservation_id
            if donor_reservation_id is None:
                raise BusinessRuleError(
                    "The donor sales-order line has no linked reservation.",
                    details={"sales_order_line_id": str(donor_target.line.id)},
                )
            reservation_ids.append(donor_reservation_id)
        if recipient_line is not None and recipient_line.reservation_id is not None:
            reservation_ids.append(recipient_line.reservation_id)
        locked_reservations = await engine.reservations.lock_reservations(reservation_ids)
        donor = locked_reservations.get(request.from_reservation_id)
        if donor is None:
            raise NotFoundError("Stock reservation", request.from_reservation_id)

        amount = to_quantity(request.quantity)
        if donor_target is not None:
            donor_line = donor_target.line
            donor_remaining = remaining_of(
                donor.quantity, donor.consumed_quantity, donor.released_quantity
            )
            if (
                donor_line.reservation_id != donor.id
                or to_quantity(donor_line.allocated_quantity) != donor_remaining
            ):
                raise BusinessRuleError(
                    "The donor sales-order line and reservation disagree about allocated quantity.",
                    details={
                        "sales_order_line_id": str(donor_line.id),
                        "line_allocated": str(donor_line.allocated_quantity),
                        "reservation_remaining": str(donor_remaining),
                    },
                )
        if (
            recipient_line is not None
            and recipient_line.reservation_id is None
            and to_quantity(recipient_line.allocated_quantity) != ZERO
        ):
            raise BusinessRuleError(
                "The recipient sales-order line has allocated quantity but no reservation.",
                details={"sales_order_line_id": str(recipient_line.id)},
            )
        existing_recipient = (
            locked_reservations.get(recipient_line.reservation_id)
            if recipient_line is not None and recipient_line.reservation_id is not None
            else None
        )
        if recipient_line is not None and recipient_line.reservation_id is not None:
            if existing_recipient is None:
                raise NotFoundError("Stock reservation", recipient_line.reservation_id)
            if existing_recipient.id == donor.id:
                raise BusinessRuleError("The donor cannot also be the recipient reservation.")
            if (
                existing_recipient.demand_type is not DemandType.SALES_ORDER
                or existing_recipient.demand_id != request.to_demand_id
                or existing_recipient.demand_line_id != recipient_line.id
                or existing_recipient.item_id != request.item_id
                or existing_recipient.warehouse_id != request.warehouse_id
                or existing_recipient.status not in OPEN_RESERVATION_STATUSES
            ):
                raise BusinessRuleError(
                    "The sales-order line's current reservation does not match this demand.",
                    details={"sales_order_line_id": str(recipient_line.id)},
                )
            existing_remaining = remaining_of(
                existing_recipient.quantity,
                existing_recipient.consumed_quantity,
                existing_recipient.released_quantity,
            )
            if existing_remaining != to_quantity(recipient_line.allocated_quantity):
                raise BusinessRuleError(
                    "The sales-order line and its reservation disagree about allocated quantity.",
                    details={
                        "sales_order_line_id": str(recipient_line.id),
                        "line_allocated": str(recipient_line.allocated_quantity),
                        "reservation_remaining": str(existing_remaining),
                    },
                )

        plan = plan_release(
            quantity=donor.quantity,
            consumed_quantity=donor.consumed_quantity,
            released_quantity=donor.released_quantity,
            status=donor.status,
            requested=amount,
        )
        donor_previous = donor.status
        RESERVATION_FLOW.assert_transition(donor_previous, plan.next_status)

        release_note = (
            f"Reallocated to {request.to_demand_type} "
            f"{request.to_demand_code or request.to_demand_id}: {request.reason}"
        )[:255]

        engine.apply_drawdown(balance, plan.applied_quantity)
        donor.released_quantity = to_quantity(
            to_quantity(donor.released_quantity) + plan.applied_quantity
        )
        donor.status = plan.next_status
        donor.released_by_id = approved_by
        donor.released_at = utcnow()
        donor.release_reason = release_note
        engine.reservations.touch(donor)
        if donor_target is not None:
            from src.modules.sales.service import release_reallocation_from_line

            await release_reallocation_from_line(
                session,
                tenant,
                donor_target,
                reservation_id=donor.id,
                reservation_remaining=plan.remaining_after,
                quantity=plan.applied_quantity,
                reallocation_request_id=request.id,
            )

        # ASSUMPTION: the granted reservation is HARD, and inherits the
        # requester's urgency from the request's impact summary. A person
        # approved this specific transfer of a commitment; making the result a
        # soft placeholder would let the next request take it back without
        # anybody approving that too.
        requester = (request.impact_summary or {}).get("requester") or {}
        recipient = existing_recipient
        if recipient is None:
            recipient = StockReservation(
                item_id=donor.item_id,
                warehouse_id=donor.warehouse_id,
                location_id=donor.location_id,
                batch_id=donor.batch_id,
                demand_type=request.to_demand_type,
                demand_id=request.to_demand_id,
                demand_line_id=_recipient_demand_line_id(request),
                demand_code=request.to_demand_code,
                reservation_type=ReservationType.HARD,
                quantity=amount,
                consumed_quantity=ZERO,
                released_quantity=ZERO,
                status=ReservationStatus.ACTIVE,
                priority=_parse_priority(requester.get("priority")),
                required_date=_parse_iso_date(requester.get("required_date")),
                reason=f"Reallocated from {donor.demand_type} "
                f"{donor.demand_code or donor.demand_id}: {request.reason}"[:255],
            )
            engine.reservations.add(recipient)
        else:
            recipient.quantity = to_quantity(Decimal(recipient.quantity) + amount)
            recipient.reservation_type = ReservationType.HARD
            recipient.priority = min(recipient.priority, _parse_priority(requester.get("priority")))
            requested_date = _parse_iso_date(requester.get("required_date"))
            if requested_date is not None and (
                recipient.required_date is None or requested_date < recipient.required_date
            ):
                recipient.required_date = requested_date
            recipient.reason = (
                f"Reallocation from {donor.demand_code or donor.demand_id}: {request.reason}"
            )[:255]
        engine.reservations.touch(recipient)
        engine.apply_reserve(balance, amount, item=item, warehouse_code=warehouse_code)
        # Flushed before the impact summary is written: the primary key is a
        # Python-side column default, so `recipient.id` is None until the INSERT
        # is emitted, and stamping "None" into the summary would lose the only
        # link from the request to the reservation it created.
        await session.flush()

        if recipient_target is not None:
            from src.modules.sales.service import apply_reallocation_to_line

            await apply_reallocation_to_line(
                session,
                tenant,
                recipient_target,
                reservation_id=recipient.id,
                quantity=amount,
                reallocation_request_id=request.id,
            )

        request.status = ReallocationStatus.APPROVED
        request.approved_by_id = approved_by
        request.approved_at = utcnow()
        request.decision_note = decision_note
        REALLOCATION_FLOW.assert_transition(request.status, ReallocationStatus.EXECUTED)
        request.status = ReallocationStatus.EXECUTED
        request.impact_summary = {
            **(request.impact_summary or {}),
            "executed": {
                "to_reservation_id": str(recipient.id),
                "donor_status": str(plan.next_status),
                "donor_remaining_quantity": str(plan.remaining_after),
                "quantity": str(amount),
                "at": utcnow().isoformat(),
            },
        }
        engine.requests.touch(request)
        await session.flush()

        # The donor lost stock and the recipient gained a promise, and both of
        # those are reservation events in their own right. Auditing only the
        # request would leave "what happened to this reservation?" - the
        # question the allocation screen and every shortage investigation
        # actually asks - answerable for a release done by hand but not for the
        # identical release done through an approval. Written in this
        # transaction, like every other reservation change.
        await engine.audit_reservation(
            donor,
            summary=(
                f"Released {plan.applied_quantity} of reservation for "
                f"{donor.demand_type} {donor.demand_code or donor.demand_id} by reallocation "
                f"to {request.to_demand_type} {request.to_demand_code or request.to_demand_id}"
            ),
            context={
                "released_now": plan.applied_quantity,
                "remaining_before": plan.remaining_before,
                "remaining_after": plan.remaining_after,
                "balance_reserved_after": balance.reserved_quantity,
                "release_reason": release_note,
                "reallocation_request_id": request.id,
                "to_reservation_id": recipient.id,
                "approved_by_id": approved_by,
            },
        )
        if plan.next_status is not donor_previous:
            await record_status_change(
                session,
                company_id=tenant.company_id,
                entity_type=AUDIT_ENTITY_RESERVATION,
                entity_id=donor.id,
                entity_code=donor.demand_code,
                from_status=str(donor_previous),
                to_status=str(plan.next_status),
                user_id=tenant.user_id,
                action=AuditAction.RESERVATION_CHANGE,
                reason=release_note,
            )
        await engine.audit_reservation(
            recipient,
            summary=(
                f"Reserved {amount} {item.code} at {warehouse_code} for "
                f"{request.to_demand_type} {request.to_demand_code or request.to_demand_id} "
                f"by reallocation from {donor.demand_type} "
                f"{donor.demand_code or donor.demand_id}"
            ),
            context={
                "item_code": item.code,
                "warehouse_code": warehouse_code,
                "balance_reserved_after": balance.reserved_quantity,
                "reallocation_request_id": request.id,
                "from_reservation_id": donor.id,
                "approved_by_id": approved_by,
                "reason": recipient.reason,
            },
        )

        summary = (
            f"Reallocated {amount} {item.code} at {warehouse_code} from "
            f"{donor.demand_type} {donor.demand_code or donor.demand_id} to "
            f"{request.to_demand_type} {request.to_demand_code or request.to_demand_id}"
        )
        await record_audit(
            session,
            company_id=tenant.company_id,
            action=AuditAction.REALLOCATION,
            entity_type=AUDIT_ENTITY_REALLOCATION,
            entity_id=request.id,
            entity_code=request.to_demand_code,
            summary=summary,
            user_id=tenant.user_id,
            old_values={"status": str(ReallocationStatus.PENDING)},
            new_values={"status": str(ReallocationStatus.EXECUTED)},
            context={
                "item_code": item.code,
                "warehouse_code": warehouse_code,
                "quantity": amount,
                "from_reservation_id": donor.id,
                "from_demand_type": str(donor.demand_type),
                "from_demand_id": donor.demand_id,
                "from_demand_code": donor.demand_code,
                "from_reservation_type": str(donor.reservation_type),
                "donor_status_after": str(plan.next_status),
                "donor_remaining_after": plan.remaining_after,
                "to_reservation_id": recipient.id,
                "to_demand_type": str(request.to_demand_type),
                "to_demand_id": request.to_demand_id,
                "approved_by_id": approved_by,
                "decision_note": decision_note,
                "reason": request.reason,
                "balance_reserved_after": balance.reserved_quantity,
            },
        )
        await _notify_donor_owner(
            session,
            tenant,
            donor=donor,
            request=request,
            item_code=item.code,
            quantity=amount,
            remaining=plan.remaining_after,
        )
        await _notify_requester_decision(
            session,
            tenant,
            request=request,
            item_code=item.code,
            warehouse_code=warehouse_code,
            quantity=amount,
            approved=True,
            decision_note=decision_note,
            donor_label=f"{donor.demand_type} {donor.demand_code or donor.demand_id}",
        )
    return request


# -----------------------------------------------------------------------------
# Housekeeping
# -----------------------------------------------------------------------------


async def expire_stale_reservations(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    limit: int = MAX_EXPIRY_BATCH,
    reason: str = "Reservation passed its expiry date",
) -> list[uuid.UUID]:
    """Release reservations whose expiry has passed, and mark them EXPIRED.

    The entry point for ``maintenance.release_stale_reservations``
    (``docs/assumptions.md`` section 10). Stale reservations are expensive in a
    specific way: they hide stock from availability, which makes MRP order
    material the company already owns.

    Bounded by ``limit`` on purpose. A sweep that tried to clear a year of
    backlog in one transaction would hold hundreds of balance locks while every
    live order waited behind it; the job runs again on its next tick instead.
    """
    engine = ReservationEngine(session, tenant)
    moment = utcnow()

    stale = await engine.reservations.due_for_expiry(moment, limit=min(limit, MAX_EXPIRY_BATCH))
    if not stale:
        return []

    expired: list[uuid.UUID] = []
    #: Reservations this pass could not expire without violating an invariant.
    #: Reported rather than raised - see the guard inside the loop.
    skipped: list[uuid.UUID] = []
    #: The status each expired row held before the sweep, captured while it is
    #: still on the row so the lifecycle audit entry can say what it moved from.
    previous_status: dict[uuid.UUID, ReservationStatus] = {}
    async with transaction(session):
        # Every affected position is locked in one ordered acquisition, then
        # every reservation, so the sweep cannot deadlock against a live order
        # touching the same items.
        grains = [
            BalanceGrain(
                item_id=row.item_id,
                warehouse_id=row.warehouse_id,
                location_id=row.location_id,
                batch_id=row.batch_id,
            )
            for row in stale
        ]
        balances = await engine.movements.lock(grains)
        locked = await engine.reservations.lock_reservations([row.id for row in stale])

        for grain, row_id in zip(grains, [row.id for row in stale], strict=True):
            reservation = locked.get(row_id)
            if reservation is None or reservation.status not in OPEN_RESERVATION_STATUSES:
                # Someone closed it between the unlocked read and the lock. That
                # is the lock doing its job, not an error.
                continue

            # A row this sweep cannot expire is skipped, never raised on. The
            # whole batch shares one transaction, so letting plan_release or
            # apply_drawdown raise here would roll back every *other*
            # reservation in it - and because due_for_expiry always returns the
            # oldest first, the same bad row would head every subsequent batch.
            # One reservation whose balance has drifted out of step would then
            # stop this tenant's reservations from ever expiring again, which is
            # precisely the stock-hidden-from-availability problem the job
            # exists to prevent.
            outstanding = remaining_of(
                reservation.quantity,
                reservation.consumed_quantity,
                reservation.released_quantity,
            )
            held = to_quantity(balances[grain].reserved_quantity)
            if outstanding <= ZERO or outstanding > held:
                skipped.append(row_id)
                log.warning(
                    "allocation.expiry_skipped_inconsistent_reservation",
                    company_id=str(tenant.company_id),
                    reservation_id=str(row_id),
                    outstanding=str(outstanding),
                    balance_reserved=str(held),
                )
                continue

            plan = plan_release(
                quantity=reservation.quantity,
                consumed_quantity=reservation.consumed_quantity,
                released_quantity=reservation.released_quantity,
                status=reservation.status,
            )
            RESERVATION_FLOW.assert_transition(reservation.status, ReservationStatus.EXPIRED)
            previous_status[reservation.id] = reservation.status
            engine.apply_drawdown(balances[grain], plan.applied_quantity)
            reservation.released_quantity = to_quantity(
                to_quantity(reservation.released_quantity) + plan.applied_quantity
            )
            reservation.status = ReservationStatus.EXPIRED
            reservation.released_at = moment
            reservation.release_reason = reason[:255]
            engine.reservations.touch(reservation)
            expired.append(reservation.id)

        if expired:
            await session.flush()
            for row_id in expired:
                reservation = locked[row_id]
                await engine.audit_reservation(
                    reservation,
                    summary=(
                        f"Expired reservation for {reservation.demand_type} "
                        f"{reservation.demand_code or reservation.demand_id} ({reason})"
                    ),
                    context={"expires_at": reservation.expires_at, "release_reason": reason},
                )
                # The lifecycle row every other reservation transition writes
                # (release, consume, reallocation): an auditor filtering the log
                # on ``changed_fields = ["status"]`` must see expiries too, not
                # only the free-text summary above.
                await record_status_change(
                    session,
                    company_id=tenant.company_id,
                    entity_type=AUDIT_ENTITY_RESERVATION,
                    entity_id=reservation.id,
                    entity_code=reservation.demand_code,
                    from_status=str(previous_status[row_id]),
                    to_status=str(ReservationStatus.EXPIRED),
                    user_id=tenant.user_id,
                    action=AuditAction.RESERVATION_CHANGE,
                    reason=reason,
                )

    log.info(
        "allocation.reservations_expired",
        company_id=str(tenant.company_id),
        expired=len(expired),
        skipped=len(skipped),
        examined=len(stale),
    )
    return expired


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _parse_priority(value: object) -> int:
    """Read the requester's urgency back out of the request's JSONB context.

    Written as an explicit "is it missing?" test rather than ``value or
    DEFAULT``, because ``0`` is both a legal priority and the *most urgent* one.
    A truthiness fallback silently rewrites the rush order this reallocation was
    approved for into a default-priority reservation, which any demand at
    priority 99 or better could then take straight back - without anybody
    approving that second loss.

    Anything unreadable degrades to the default rather than aborting an approved
    reallocation: the priority is ranking metadata, and the ``priority >= 0``
    check constraint is the floor that must not be crossed.
    """
    # bool is an int subclass, and ``True`` is not a priority.
    if isinstance(value, bool):
        return DEFAULT_RESERVATION_PRIORITY
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            log.warning("allocation.unparseable_priority", value=value)
            return DEFAULT_RESERVATION_PRIORITY
    else:
        return DEFAULT_RESERVATION_PRIORITY
    if parsed < 0:
        log.warning("allocation.negative_priority", value=parsed)
        return DEFAULT_RESERVATION_PRIORITY
    return parsed


def _parse_iso_date(value: object) -> date | None:
    """Read a date back out of the request's JSONB context, tolerantly.

    JSONB round-trips a date as a string. A malformed value must not abort an
    approved reallocation - the date is ranking metadata, not an invariant - so
    an unreadable one degrades to "no required date" and the reservation is still
    created.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:  # pragma: no cover - defensive against hand-edited JSON
            log.warning("allocation.unparseable_required_date", value=value)
    return None


def _assert_separate_approver(
    request: ReallocationRequest, tenant: TenantContext, approved_by: uuid.UUID
) -> None:
    # ASSUMPTION: the approver may not be the person who raised the request,
    # unless they are a platform administrator. This applies the project-wide
    # segregation-of-duties decision already recorded in docs/assumptions.md
    # section 2 to reallocations, which is where it matters most: the whole
    # control is that somebody other than the requester agrees the losing demand
    # can be disappointed.
    if tenant.is_superuser:
        return
    if request.requested_by_id is not None and request.requested_by_id == approved_by:
        raise BusinessRuleError(
            "A reallocation must be approved by someone other than the person who requested it.",
            details={"request_id": str(request.id)},
        )


async def _notify_donor_owner(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    donor: StockReservation,
    request: ReallocationRequest,
    item_code: str,
    quantity: Decimal,
    remaining: Decimal,
) -> None:
    """Tell whoever committed the donor stock that it has been taken.

    ASSUMPTION: "the donor demand's owner" is the user who created the donor
    reservation. The demand document lives in one of six modules, several of
    which this module must not import, so the owner recorded on the sales or
    production order is not reachable from here. The person who reserved the
    stock is the contract-visible equivalent, and is in practice the planner who
    needs to know. When a module later wants a different recipient it should
    raise its own notification from its own side rather than allocation growing a
    dependency on it.
    """
    if donor.created_by_id is None:
        log.info(
            "allocation.reallocation_no_owner_to_notify",
            reservation_id=str(donor.id),
            request_id=str(request.id),
        )
        return

    await notify(
        session,
        company_id=tenant.company_id,
        user_ids=[donor.created_by_id],
        notification_type=NotificationType.ALLOCATION_CHANGED,
        # WARNING, not INFO: the recipient's order has lost material it was
        # promised, and they may need to re-plan.
        severity=NotificationSeverity.WARNING,
        title=f"{quantity} {item_code} reallocated away from "
        f"{donor.demand_code or donor.demand_type}",
        body=(
            f"{quantity} {item_code} reserved for {donor.demand_type} "
            f"{donor.demand_code or donor.demand_id} has been reallocated to "
            f"{request.to_demand_type} {request.to_demand_code or request.to_demand_id}. "
            f"Reason: {request.reason}. "
            f"{remaining} remains reserved for the original demand."
        ),
        entity_type=AUDIT_ENTITY_RESERVATION,
        entity_id=donor.id,
        action_url=f"/allocation/reservations/{donor.id}",
        # One bell row per donor reservation: a demand losing stock three times
        # in an afternoon is one situation, not three alerts.
        group_key=f"reallocation:{donor.id}",
        actor_id=tenant.user_id,
    )


async def _notify_requester_decision(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    request: ReallocationRequest,
    item_code: str,
    warehouse_code: str,
    quantity: Decimal,
    approved: bool,
    decision_note: str | None,
    donor_label: str | None = None,
) -> None:
    """Tell the person who raised the request what happened to it.

    The approval inbox is pull-based; a requester who raised a request for an
    urgent order should not have to keep refreshing it to discover the stock was
    approved or that they need another source. The notification is raised in the
    decision transaction, so it cannot announce a decision that then rolls back.

    Server-side permissions still gate the inbox and the decision endpoints; this
    is a courtesy signal, not the authority record. The audit entry remains the
    authoritative history.
    """
    recipient = request.requested_by_id
    if recipient is None or recipient == tenant.user_id:
        # A superuser approving their own request does not need a notification
        # about a decision they just took.
        return

    target = f"{request.to_demand_type} {request.to_demand_code or request.to_demand_id}"
    verb = "approved" if approved else "rejected"
    title = f"Reallocation {verb}: {quantity} {item_code}"
    if approved:
        body = (
            f"Your reallocation request for {quantity} {item_code} at "
            f"{warehouse_code} for {target} was approved and executed. "
            f"The stock was taken from {donor_label or 'the selected donor'}."
        )
    else:
        body = (
            f"Your reallocation request for {quantity} {item_code} at "
            f"{warehouse_code} for {target} was rejected. "
            "The donor's stock was not moved."
        )
    body += f" Decision note: {decision_note}." if decision_note else " No decision note was given."

    await notify(
        session,
        company_id=tenant.company_id,
        user_ids=[recipient],
        notification_type=(
            NotificationType.APPROVAL_GRANTED if approved else NotificationType.APPROVAL_REJECTED
        ),
        severity=NotificationSeverity.INFO if approved else NotificationSeverity.WARNING,
        title=title,
        body=body,
        entity_type=AUDIT_ENTITY_REALLOCATION,
        entity_id=request.id,
        action_url="/inventory/reservations",
        # One decision per request; the group key still prevents a duplicate
        # row if a retried request re-runs the notifier after a transient error.
        group_key=f"reallocation-decision:{request.id}",
        actor_id=tenant.user_id,
    )


# -----------------------------------------------------------------------------
# Router-facing service
# -----------------------------------------------------------------------------


class AllocationService:
    """Everything the HTTP surface calls. Rules live below it, never above."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.engine = ReservationEngine(session, tenant)
        self.reservations = self.engine.reservations
        self.requests = self.engine.requests
        self.inventory = self.engine.inventory

    # --- reservations -------------------------------------------------------

    async def list_reservations(
        self, filters: ReservationFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ReservationRead], OffsetPagination]:
        stmt = self.reservations.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)
        return await self._to_reads(rows), pagination

    async def get_reservation(self, reservation_id: uuid.UUID) -> ReservationRead:
        reservation = await self.reservations.get(reservation_id)
        if reservation is None:
            raise NotFoundError("Stock reservation", reservation_id)
        return (await self._to_reads([reservation]))[0]

    async def create_reservation(self, payload: ReservationCreate) -> ReservationRead:
        reservation = await reserve(
            self.session,
            self.tenant,
            item_id=payload.item_id,
            warehouse_id=payload.warehouse_id,
            quantity=payload.quantity,
            demand_type=payload.demand_type,
            demand_id=payload.demand_id,
            demand_line_id=payload.demand_line_id,
            demand_code=payload.demand_code,
            reservation_type=payload.reservation_type,
            priority=payload.priority,
            required_date=payload.required_date,
            expires_at=payload.expires_at,
            reason=payload.reason,
            location_id=payload.location_id,
            batch_id=payload.batch_id,
        )
        return (await self._to_reads([reservation]))[0]

    async def release_reservation(
        self, reservation_id: uuid.UUID, *, reason: str, quantity: Decimal | None
    ) -> ReservationRead:
        reservation = await release(
            self.session,
            self.tenant,
            reservation_id=reservation_id,
            reason=reason,
            quantity=quantity,
        )
        return (await self._to_reads([reservation]))[0]

    async def availability(
        self, item_id: uuid.UUID, warehouse_id: uuid.UUID | None
    ) -> AvailableToPromiseRead:
        quantity = await available_to_promise(
            self.session, self.tenant, item_id=item_id, warehouse_id=warehouse_id
        )
        return AvailableToPromiseRead(
            item_id=item_id,
            warehouse_id=warehouse_id,
            available_quantity=to_quantity(quantity),
            as_of=utcnow(),
        )

    # --- reallocation -------------------------------------------------------

    async def list_reallocation_requests(
        self, filters: ReallocationFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[ReallocationRequestRead], OffsetPagination]:
        stmt = self.requests.build_list_stmt(filters, sort)
        rows, pagination = await paginate(self.session, stmt, params)
        return await self._to_request_reads(rows), pagination

    async def get_reallocation_request(self, request_id: uuid.UUID) -> ReallocationRequestRead:
        request = await self.requests.get(request_id)
        if request is None:
            raise NotFoundError("Reallocation request", request_id)
        return (await self._to_request_reads([request]))[0]

    async def create_reallocation_request(
        self, payload: ReallocationRequestCreate, *, policy: ReallocationPolicy = DEFAULT_POLICY
    ) -> ReallocationRequestRead:
        """Suggest donors and raise the request against the chosen one.

        One call because they are one decision. Ranking the donors and then
        asking the client to post the winner back would leave a window in which
        the chosen reservation is consumed by somebody else, and would let a
        client raise a request against a donor the engine never offered.

        The ranking *and* the insert are inside one transaction, and the chosen
        donor's row is locked before the quantity is settled. Without the lock
        the "is this donor still unclaimed?" test is a check-then-act: two
        planners raising a request at the same moment both read no claims, both
        promise themselves the same 100 units, and the second approval fails
        after somebody has already signed for it - with the donor now reading as
        fully claimed to every request that follows.
        """
        async with transaction(self.session):
            candidates = await suggest_reallocation(
                self.session,
                self.tenant,
                item_id=payload.item_id,
                warehouse_id=payload.warehouse_id,
                quantity_needed=payload.quantity,
                for_demand_type=payload.to_demand_type,
                for_demand_id=payload.to_demand_id,
                required_date=payload.required_date,
                priority=payload.priority,
                policy=policy,
                prefer_reservation_id=payload.from_reservation_id,
            )

            chosen = self._choose_donor(candidates, payload)
            # One donor can only give what it is holding. Asking for more than
            # the best candidate has is not an error - the shortfall is simply
            # covered in part, and the caller raises a second request against
            # the next donor.
            amount = min(
                to_quantity(payload.quantity),
                chosen.takeable_quantity,
                await self._claimable_under_lock(chosen),
            )
            if amount <= ZERO:  # pragma: no cover - _claimable_under_lock raises first
                raise BusinessRuleError(
                    "That reservation has nothing left to give.",
                    code=ErrorCode.INSUFFICIENT_AVAILABLE_STOCK,
                    details={"from_reservation_id": str(chosen.reservation_id)},
                )

            request = ReallocationRequest(
                from_reservation_id=chosen.reservation_id,
                to_demand_type=payload.to_demand_type,
                to_demand_id=payload.to_demand_id,
                to_demand_code=payload.to_demand_code,
                item_id=payload.item_id,
                warehouse_id=payload.warehouse_id,
                quantity=amount,
                reason=payload.reason,
                status=ReallocationStatus.PENDING,
                requested_by_id=self.tenant.user_id,
                impact_summary=_impact_summary(chosen, payload, amount, len(candidates)),
            )
            self.requests.add(request)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.REALLOCATION,
                entity_type=AUDIT_ENTITY_REALLOCATION,
                entity_id=request.id,
                entity_code=payload.to_demand_code,
                summary=(
                    f"Requested {amount} from {chosen.demand_type} "
                    f"{chosen.demand_code or chosen.demand_id} for "
                    f"{payload.to_demand_type} {payload.to_demand_code or payload.to_demand_id}"
                ),
                user_id=self.tenant.user_id,
                new_values={
                    "status": str(ReallocationStatus.PENDING),
                    "quantity": amount,
                    "from_reservation_id": chosen.reservation_id,
                },
                context={
                    "reason": payload.reason,
                    "requires_approval": chosen.requires_approval,
                    "candidates_considered": len(candidates),
                    "donor_reservation_type": str(chosen.reservation_type),
                },
            )

        read = (await self._to_request_reads([request]))[0]
        read.candidates = [_candidate_read(candidate) for candidate in candidates]
        return read

    async def approve_reallocation_request(
        self, request_id: uuid.UUID, *, decision_note: str | None
    ) -> ReallocationRequestRead:
        """Approve and execute. See :func:`reallocate` for why they are one step."""
        request = await reallocate(
            self.session,
            self.tenant,
            request_id=request_id,
            approved_by=self.tenant.user_id,
            decision_note=decision_note,
        )
        return (await self._to_request_reads([request]))[0]

    async def reject_reallocation_request(
        self, request_id: uuid.UUID, *, decision_note: str | None
    ) -> ReallocationRequestRead:
        async with transaction(self.session):
            request = await self.requests.lock_request(request_id)
            if request is None:
                raise NotFoundError("Reallocation request", request_id)
            REALLOCATION_FLOW.assert_transition(request.status, ReallocationStatus.REJECTED)
            _assert_separate_approver(request, self.tenant, self.tenant.user_id)

            previous = request.status
            request.status = ReallocationStatus.REJECTED
            request.approved_by_id = self.tenant.user_id
            request.approved_at = utcnow()
            request.decision_note = decision_note
            self.requests.touch(request)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_REALLOCATION,
                entity_id=request.id,
                entity_code=request.to_demand_code,
                from_status=str(previous),
                to_status=str(ReallocationStatus.REJECTED),
                user_id=self.tenant.user_id,
                action=AuditAction.REJECT,
                reason=decision_note,
            )
            item, warehouse_code = await self.engine.resolve_position(
                request.item_id, request.warehouse_id
            )
            await _notify_requester_decision(
                self.session,
                self.tenant,
                request=request,
                item_code=item.code,
                warehouse_code=warehouse_code,
                quantity=to_quantity(request.quantity),
                approved=False,
                decision_note=decision_note,
            )
        return (await self._to_request_reads([request]))[0]

    # --- serialisation ------------------------------------------------------

    async def _claimable_under_lock(self, chosen: ReallocationCandidate) -> Decimal:
        """How much of the chosen donor a new request may still ask for.

        The ranking read the donor without a lock, which is right for a screen
        and wrong for the decision that follows it. This re-reads the same two
        facts - what the reservation still holds, and what open requests have
        already promised away - with the reservation row held ``FOR UPDATE``, so
        a second request racing for the same donor queues here and then sees the
        first one's claim.

        Only the reservation lock is taken. The module's ordering rule is
        balance-then-reservation; taking the second class alone can never invert
        that order, and nothing further is acquired while it is held.
        """
        locked = await self.reservations.lock_reservations([chosen.reservation_id])
        donor = locked.get(chosen.reservation_id)
        if donor is None:
            raise NotFoundError("Stock reservation", chosen.reservation_id)
        if donor.status not in OPEN_RESERVATION_STATUSES:
            raise BusinessRuleError(
                f"That reservation is {donor.status} and can no longer give up stock.",
                details={
                    "from_reservation_id": str(donor.id),
                    "status": str(donor.status),
                },
            )

        remaining = remaining_of(donor.quantity, donor.consumed_quantity, donor.released_quantity)
        claims = await self.requests.claimed_quantities([donor.id])
        unclaimed = to_quantity(remaining - claims.get(donor.id, ZERO))
        if unclaimed <= ZERO:
            raise BusinessRuleError(
                "That reservation is already promised in full to another reallocation request.",
                code=ErrorCode.INSUFFICIENT_AVAILABLE_STOCK,
                details={
                    "from_reservation_id": str(donor.id),
                    "remaining": str(remaining),
                    "already_claimed": str(claims.get(donor.id, ZERO)),
                },
            )
        return unclaimed

    @staticmethod
    def _choose_donor(
        candidates: list[ReallocationCandidate], payload: ReallocationRequestCreate
    ) -> ReallocationCandidate:
        if payload.from_reservation_id is not None:
            for candidate in candidates:
                if candidate.reservation_id == payload.from_reservation_id:
                    return candidate
            # Naming a donor the engine did not offer is refused rather than
            # honoured: the reservation is either more urgent than the requester,
            # already claimed, or inside its protection window, and the request
            # exists precisely so those rules are not bypassable by id.
            raise BusinessRuleError(
                "That reservation is not an eligible donor for this demand.",
                details={"from_reservation_id": str(payload.from_reservation_id)},
            )
        if not candidates:
            raise BusinessRuleError(
                "No reservation can give up stock for this demand: either the stock is "
                "already available, or every holder is at least as urgent.",
                code=ErrorCode.INSUFFICIENT_AVAILABLE_STOCK,
                details={
                    "item_id": str(payload.item_id),
                    "warehouse_id": str(payload.warehouse_id),
                    "quantity": str(payload.quantity),
                },
            )
        return candidates[0]

    async def _to_reads(self, rows: list[StockReservation]) -> list[ReservationRead]:
        """Attach item and warehouse labels with two batched queries, never per row."""
        if not rows:
            return []
        items = await self.inventory.item_facts([row.item_id for row in rows])
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])

        reads: list[ReservationRead] = []
        for row in rows:
            read = ReservationRead.model_validate(row)
            item = items.get(row.item_id)
            warehouse = warehouses.get(row.warehouse_id)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            read.warehouse_code = warehouse.code if warehouse else None
            # From the stored columns rather than the generated one, so the value
            # is right even in the same transaction that just changed them.
            read.remaining_quantity = remaining_of(
                row.quantity, row.consumed_quantity, row.released_quantity
            )
            reads.append(read)
        return reads

    async def _to_request_reads(
        self, rows: list[ReallocationRequest]
    ) -> list[ReallocationRequestRead]:
        if not rows:
            return []
        items = await self.inventory.item_facts([row.item_id for row in rows])
        warehouses = await self.inventory.warehouse_facts([row.warehouse_id for row in rows])
        donors = {
            donor.id: donor
            for donor in await self.reservations.get_many([row.from_reservation_id for row in rows])
        }

        reads: list[ReallocationRequestRead] = []
        for row in rows:
            read = ReallocationRequestRead.model_validate(row)
            item = items.get(row.item_id)
            warehouse = warehouses.get(row.warehouse_id)
            read.item_code = item.code if item else None
            read.item_name = item.name if item else None
            read.warehouse_code = warehouse.code if warehouse else None

            donor = donors.get(row.from_reservation_id)
            if donor is not None:
                read.from_demand_type = donor.demand_type
                read.from_demand_id = donor.demand_id
                read.from_demand_code = donor.demand_code
                read.from_reservation_type = donor.reservation_type

            executed = (row.impact_summary or {}).get("executed") or {}
            created = executed.get("to_reservation_id")
            if isinstance(created, str):
                read.to_reservation_id = uuid.UUID(created)
            requester = (row.impact_summary or {}).get("requester") or {}
            demand_line_id = requester.get("demand_line_id")
            if isinstance(demand_line_id, str):
                read.to_demand_line_id = uuid.UUID(demand_line_id)
            reads.append(read)
        return reads


def _candidate_read(candidate: ReallocationCandidate) -> ReallocationCandidateRead:
    return ReallocationCandidateRead(
        reservation_id=candidate.reservation_id,
        item_id=candidate.item_id,
        warehouse_id=candidate.warehouse_id,
        demand_type=candidate.demand_type,
        demand_id=candidate.demand_id,
        demand_code=candidate.demand_code,
        reservation_type=candidate.reservation_type,
        priority=candidate.priority,
        required_date=candidate.required_date,
        quantity=candidate.quantity,
        consumed_quantity=candidate.consumed_quantity,
        remaining_quantity=candidate.remaining_quantity,
        takeable_quantity=candidate.takeable_quantity,
        requires_approval=candidate.requires_approval,
        score=candidate.score,
        reasons=list(candidate.reasons),
    )


def _impact_summary(
    chosen: ReallocationCandidate,
    payload: ReallocationRequestCreate,
    amount: Decimal,
    considered: int,
) -> dict[str, Any]:
    """What the approver is being asked to sign, captured when they are asked.

    ``donor`` is the contract's meaning of ``impact_summary``: what the losing
    demand looks like if this goes ahead. ``requester`` carries the small amount
    of context the fixed column list has no home for - the urgency and date the
    granted reservation must inherit, which would otherwise be lost between the
    request and its execution.

    Recomputing any of this at approval time would show the approver numbers that
    were not true when the request was raised, and a queue of requests whose
    stated impact shifts under the reader is a queue nobody trusts.
    """
    return {
        "donor": {
            "reservation_id": str(chosen.reservation_id),
            "demand_type": str(chosen.demand_type),
            "demand_id": str(chosen.demand_id),
            "demand_code": chosen.demand_code,
            "reservation_type": str(chosen.reservation_type),
            "priority": chosen.priority,
            "required_date": chosen.required_date.isoformat() if chosen.required_date else None,
            "quantity_before": str(chosen.remaining_quantity),
            "quantity_taken": str(amount),
            "quantity_after": str(to_quantity(chosen.remaining_quantity - amount)),
            "already_issued": str(chosen.consumed_quantity),
            "requires_approval": chosen.requires_approval,
            "reasons": list(chosen.reasons),
        },
        "requester": {
            "demand_type": str(payload.to_demand_type),
            "demand_id": str(payload.to_demand_id),
            "demand_code": payload.to_demand_code,
            "priority": payload.priority,
            "required_date": (payload.required_date.isoformat() if payload.required_date else None),
            "quantity_needed": str(payload.quantity),
            "demand_line_id": str(payload.to_demand_line_id) if payload.to_demand_line_id else None,
        },
        "candidates_considered": considered,
    }


def _recipient_demand_line_id(request: ReallocationRequest) -> uuid.UUID | None:
    requester = (request.impact_summary or {}).get("requester") or {}
    raw = requester.get("demand_line_id")
    return uuid.UUID(raw) if raw else None


__all__ = [
    "AUDIT_ENTITY_REALLOCATION",
    "AUDIT_ENTITY_RESERVATION",
    "CONSUMPTION_MOVEMENT",
    "DEFAULT_POLICY",
    "MAX_EXPIRY_BATCH",
    "REALLOCATION_FLOW",
    "RESERVATION_FLOW",
    "AllocationService",
    "DrawdownPlan",
    "ReallocationCandidate",
    "ReallocationContext",
    "ReallocationPolicy",
    "ReservationEngine",
    "available_to_promise",
    "candidate_eligibility",
    "consume",
    "expire_stale_reservations",
    "plan_consume",
    "plan_release",
    "rank_candidates",
    "reallocate",
    "release",
    "remaining_of",
    "reserve",
    "score_candidate",
    "suggest_reallocation",
]
