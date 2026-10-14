"""The sales order state graph and the derived fulfilment quantities.

The lifecycle is data (``StateMachine``), so it can be asserted directly rather
than by driving a database through every transition. The quantity rules beside
it - what may be reserved, what status a line is therefore in - are the two
pieces of arithmetic every allocation decision in the module rests on.
"""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal

import pytest

from src.core.deps import Principal
from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.sales.models import (
    OPEN_DEMAND_STATUSES,
    RESERVING_STATUSES,
    SalesOrderLineStatus,
    SalesOrderStatus,
)
from src.modules.sales.service import (
    SALES_ORDER_LIFECYCLE,
    TOP_UP_STATUSES,
    can_approve,
    dispatchable_quantity,
    line_shortfall,
    line_status_for,
    live_reserved,
    reservable_quantity,
)

pytestmark = pytest.mark.unit


# -----------------------------------------------------------------------------
# The graph itself
# -----------------------------------------------------------------------------


def test_graph_has_no_unreachable_states_and_no_dead_ends():
    """``validate`` catches the two mistakes that actually happen: a state
    nothing can reach, and a non-terminal state nothing can leave."""
    SALES_ORDER_LIFECYCLE.validate()


def test_the_documented_happy_path_is_walkable_end_to_end():
    """database.md section 7, edge for edge."""
    path = [
        SalesOrderStatus.DRAFT,
        SalesOrderStatus.CONFIRMED,
        SalesOrderStatus.ALLOCATED,
        SalesOrderStatus.PARTIALLY_DISPATCHED,
        SalesOrderStatus.DISPATCHED,
        SalesOrderStatus.INVOICED,
        SalesOrderStatus.CLOSED,
    ]
    for current, target in itertools.pairwise(path):
        assert SALES_ORDER_LIFECYCLE.can_transition(current, target), f"{current} -> {target}"


@pytest.mark.parametrize(
    "source",
    [SalesOrderStatus.DRAFT, SalesOrderStatus.CONFIRMED, SalesOrderStatus.ALLOCATED],
)
def test_cancellation_is_reachable_only_before_anything_ships(source):
    assert SALES_ORDER_LIFECYCLE.can_transition(source, SalesOrderStatus.CANCELLED)


@pytest.mark.parametrize(
    "source",
    [
        SalesOrderStatus.PARTIALLY_DISPATCHED,
        SalesOrderStatus.DISPATCHED,
        SalesOrderStatus.INVOICED,
        SalesOrderStatus.CLOSED,
    ],
)
def test_a_dispatched_order_cannot_be_cancelled(source):
    """Stock has moved. Cancelling would leave the inventory ledger describing a
    movement against a document that claims never to have existed."""
    with pytest.raises(InvalidStateTransitionError):
        SALES_ORDER_LIFECYCLE.assert_transition(source, SalesOrderStatus.CANCELLED)


def test_terminal_states_have_no_exit():
    for terminal in (SalesOrderStatus.CLOSED, SalesOrderStatus.CANCELLED):
        assert SALES_ORDER_LIFECYCLE.is_terminal(terminal)
        assert SALES_ORDER_LIFECYCLE.allowed_targets(terminal) == []


def test_an_order_cannot_skip_confirmation():
    with pytest.raises(InvalidStateTransitionError):
        SALES_ORDER_LIFECYCLE.assert_transition(SalesOrderStatus.DRAFT, SalesOrderStatus.ALLOCATED)


def test_a_cancelled_order_cannot_be_revived():
    with pytest.raises(InvalidStateTransitionError):
        SALES_ORDER_LIFECYCLE.assert_transition(
            SalesOrderStatus.CANCELLED, SalesOrderStatus.CONFIRMED
        )


def test_irreversible_edges_are_marked_so_the_ui_can_confirm_them():
    for source, target in (
        (SalesOrderStatus.DRAFT, SalesOrderStatus.CONFIRMED),
        (SalesOrderStatus.CONFIRMED, SalesOrderStatus.CANCELLED),
        (SalesOrderStatus.ALLOCATED, SalesOrderStatus.PARTIALLY_DISPATCHED),
        (SalesOrderStatus.CONFIRMED, SalesOrderStatus.PARTIALLY_DISPATCHED),
    ):
        transition = SALES_ORDER_LIFECYCLE.find(source, target)
        assert transition is not None and transition.is_irreversible


def test_a_partly_reserved_order_may_ship_what_it_has_reserved():
    """K08: CONFIRMED -> PARTIALLY_DISPATCHED is the dispatch module's edge."""
    edge = SALES_ORDER_LIFECYCLE.find(
        SalesOrderStatus.CONFIRMED, SalesOrderStatus.PARTIALLY_DISPATCHED
    )
    assert edge is not None
    assert edge.permission is Permission.DISPATCH_CREATE
    assert edge.label == "Dispatch"


def test_an_allocated_order_short_again_returns_to_confirmed():
    """E1-3: ALLOCATED must mean fully reserved; losing cover undoes the label."""
    edge = SALES_ORDER_LIFECYCLE.find(SalesOrderStatus.ALLOCATED, SalesOrderStatus.CONFIRMED)
    assert edge is not None
    assert edge.permission is Permission.SALES_ORDER_UPDATE
    assert not edge.is_irreversible
    # And the round trip is legal, which is what the top-up relies on.
    assert SALES_ORDER_LIFECYCLE.can_transition(
        SalesOrderStatus.CONFIRMED, SalesOrderStatus.ALLOCATED
    )


def test_the_top_up_works_on_every_status_that_still_holds_reservations():
    assert TOP_UP_STATUSES == (
        SalesOrderStatus.CONFIRMED,
        SalesOrderStatus.ALLOCATED,
        SalesOrderStatus.PARTIALLY_DISPATCHED,
    )
    assert set(TOP_UP_STATUSES) <= set(OPEN_DEMAND_STATUSES)


def test_open_demand_statuses_are_exactly_the_ones_holding_unmet_demand():
    """MRP, the demand summary and the partial index all read this tuple, so a
    change to it has to be a deliberate one."""
    assert set(OPEN_DEMAND_STATUSES) == {
        SalesOrderStatus.CONFIRMED,
        SalesOrderStatus.ALLOCATED,
        SalesOrderStatus.PARTIALLY_DISPATCHED,
    }
    assert set(RESERVING_STATUSES) <= set(OPEN_DEMAND_STATUSES)


# -----------------------------------------------------------------------------
# What may be reserved
# -----------------------------------------------------------------------------


def test_full_stock_reserves_the_whole_demand():
    assert reservable_quantity(Decimal("100"), Decimal("250")) == Decimal("100.0000")


def test_partial_stock_reserves_what_exists():
    """Master rules section 72: the order is not refused, it is short."""
    assert reservable_quantity(Decimal("100"), Decimal("40")) == Decimal("40.0000")


def test_no_stock_reserves_nothing_rather_than_a_negative_quantity():
    assert reservable_quantity(Decimal("100"), Decimal("0")) == Decimal("0.0000")
    assert reservable_quantity(Decimal("100"), Decimal("-5")) == Decimal("0.0000")


def test_reservation_is_rounded_to_the_stored_quantity_scale():
    taken = reservable_quantity(Decimal("10"), Decimal("3.123456789"))
    assert taken == Decimal("3.1235")


# -----------------------------------------------------------------------------
# What may ship (docs/assumptions.md section E1-1)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ordered", "dispatched", "reserved", "planned", "expected"),
    [
        # The proposal's own K08 case: 500 ordered, 200 reserved -> 200 may ship.
        ("500", "0", "200", "0", "200.0000"),
        # The reservation is the cap even when far more is outstanding.
        ("500", "200", "300", "0", "300.0000"),
        # Open consignments count: 400 already planned leaves 100 of 500.
        ("500", "0", "500", "400", "100.0000"),
        # Nothing reserved (none made, or released/expired/consumed) ships nothing.
        ("500", "0", "0", "0", "0.0000"),
        # A reservation larger than what is left is capped by what is left.
        ("100", "40", "80", "0", "60.0000"),
        # Never negative, however far the plan overshoots.
        ("100", "40", "80", "90", "0.0000"),
    ],
)
def test_a_line_may_ship_its_live_reservation_less_what_is_already_planned(
    ordered, dispatched, reserved, planned, expected
):
    assert dispatchable_quantity(
        ordered=Decimal(ordered),
        dispatched=Decimal(dispatched),
        reserved=Decimal(reserved),
        planned=Decimal(planned),
    ) == Decimal(expected)


def test_dispatchable_quantity_is_at_the_stored_quantity_scale():
    assert dispatchable_quantity(
        ordered=Decimal("10"), dispatched=Decimal("0"), reserved=Decimal("3.123456789")
    ) == Decimal("3.1235")


def _line(quantity: str, dispatched: str, status=SalesOrderLineStatus.PARTIALLY_ALLOCATED):
    from src.modules.sales.models import SalesOrderLine

    return SalesOrderLine(
        quantity=Decimal(quantity), dispatched_quantity=Decimal(dispatched), status=status
    )


def test_a_line_is_short_by_open_demand_less_its_live_reservation():
    assert line_shortfall(_line("500", "200"), Decimal("100")) == Decimal("200.0000")
    assert line_shortfall(_line("500", "200"), Decimal("300")) == Decimal("0.0000")
    # Over-reserved is not "negative short".
    assert line_shortfall(_line("500", "200"), Decimal("400")) == Decimal("0.0000")


def test_a_cancelled_line_is_never_short():
    cancelled = _line("500", "0", SalesOrderLineStatus.CANCELLED)
    assert line_shortfall(cancelled, Decimal("0")) == Decimal("0.0000")


def test_a_line_without_a_reservation_or_with_a_closed_one_holds_nothing():
    holder = _line("10", "0")
    holder.reservation_id = None
    assert live_reserved(holder, {}) == Decimal("0")

    holder.reservation_id = uuid.uuid4()
    # Closed reservations are absent from the live map - read as zero.
    assert live_reserved(holder, {}) == Decimal("0")
    assert live_reserved(holder, {holder.reservation_id: Decimal("7")}) == Decimal("7")


# -----------------------------------------------------------------------------
# Derived line status
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "allocated", "dispatched", "expected"),
    [
        ("100", "0", "0", SalesOrderLineStatus.PENDING),
        ("100", "40", "0", SalesOrderLineStatus.PARTIALLY_ALLOCATED),
        ("100", "100", "0", SalesOrderLineStatus.ALLOCATED),
        ("100", "100", "40", SalesOrderLineStatus.PARTIALLY_DISPATCHED),
        ("100", "0", "100", SalesOrderLineStatus.DISPATCHED),
    ],
)
def test_line_status_follows_the_quantities(quantity, allocated, dispatched, expected):
    assert (
        line_status_for(
            quantity=Decimal(quantity),
            allocated_quantity=Decimal(allocated),
            dispatched_quantity=Decimal(dispatched),
        )
        is expected
    )


def test_dispatch_outranks_allocation_in_the_derived_status():
    """A line that has shipped is not "allocated" any more, whatever the
    reservation column happens to say at that instant."""
    assert (
        line_status_for(
            quantity=Decimal("10"),
            allocated_quantity=Decimal("10"),
            dispatched_quantity=Decimal("10"),
        )
        is SalesOrderLineStatus.DISPATCHED
    )


def test_cancellation_overrides_every_other_signal():
    assert (
        line_status_for(
            quantity=Decimal("10"),
            allocated_quantity=Decimal("10"),
            dispatched_quantity=Decimal("0"),
            cancelled=True,
        )
        is SalesOrderLineStatus.CANCELLED
    )


# -----------------------------------------------------------------------------
# Segregation of duties on approval
# -----------------------------------------------------------------------------


def _principal(user_id: uuid.UUID, *, superuser: bool = False, roles: tuple[str, ...] = ()):
    return Principal(
        user_id=user_id,
        company_id=uuid.uuid4(),
        email="approver@atcraft.test",
        full_name="Approver",
        is_superuser=superuser,
        permissions=frozenset({str(Permission.SALES_ORDER_APPROVE)}),
        role_codes=roles,
    )


def test_an_author_may_not_approve_their_own_below_cost_order():
    author_id = uuid.uuid4()
    assert can_approve(_principal(author_id), author_id) is False


def test_a_second_person_may_approve():
    assert can_approve(_principal(uuid.uuid4()), uuid.uuid4()) is True


def test_super_admin_may_approve_its_own_work():
    author_id = uuid.uuid4()
    assert can_approve(_principal(author_id, roles=("SUPER_ADMIN",)), author_id) is True
    assert can_approve(_principal(author_id, superuser=True), author_id) is True
