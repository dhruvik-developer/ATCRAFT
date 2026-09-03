"""Procurement state machines.

The purchase order graph is the contract in database.md section 7. These tests
are what stops an edge being added quietly: a transition nobody declared is a
document that can reach a state no downstream module expects.

No database. The machines are declared data, which is the point of encoding them
that way rather than as ``if`` chains through the service.
"""

from __future__ import annotations

import itertools

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.procurement.models import (
    GoodsReceiptStatus,
    PurchaseOrderStatus,
    PurchaseReturnStatus,
    QuotationStatus,
    RequisitionStatus,
    RfqStatus,
)
from src.modules.procurement.service_rules import (
    GOODS_RECEIPT_LIFECYCLE,
    PURCHASE_ORDER_LIFECYCLE,
    PURCHASE_RETURN_LIFECYCLE,
    QUOTATION_LIFECYCLE,
    REQUISITION_LIFECYCLE,
    RFQ_LIFECYCLE,
)

pytestmark = pytest.mark.unit

ALL_MACHINES = [
    REQUISITION_LIFECYCLE,
    RFQ_LIFECYCLE,
    QUOTATION_LIFECYCLE,
    PURCHASE_ORDER_LIFECYCLE,
    GOODS_RECEIPT_LIFECYCLE,
    PURCHASE_RETURN_LIFECYCLE,
]


@pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.entity)
def test_every_machine_is_structurally_sound(machine):
    """No unreachable state, and no non-terminal state a document cannot leave."""
    machine.validate()


# -----------------------------------------------------------------------------
# The purchase order happy path (database.md section 7)
# -----------------------------------------------------------------------------

HAPPY_PATH = [
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING_APPROVAL,
    PurchaseOrderStatus.APPROVED,
    PurchaseOrderStatus.SENT,
    PurchaseOrderStatus.ACKNOWLEDGED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
    PurchaseOrderStatus.RECEIVED,
    PurchaseOrderStatus.QC_PENDING,
    PurchaseOrderStatus.QC_COMPLETED,
    PurchaseOrderStatus.GRN_COMPLETED,
    PurchaseOrderStatus.INVOICED,
    PurchaseOrderStatus.PAID,
    PurchaseOrderStatus.CLOSED,
]


def test_the_documented_happy_path_is_walkable_end_to_end():
    for current, target in itertools.pairwise(HAPPY_PATH):
        PURCHASE_ORDER_LIFECYCLE.assert_transition(current, target)


def test_a_draft_cannot_jump_straight_to_approved():
    """Approval without submission would skip the queue an approver works from."""
    with pytest.raises(InvalidStateTransitionError):
        PURCHASE_ORDER_LIFECYCLE.assert_transition(
            PurchaseOrderStatus.DRAFT, PurchaseOrderStatus.APPROVED
        )


def test_an_unapproved_order_cannot_be_sent_to_the_vendor():
    with pytest.raises(InvalidStateTransitionError):
        PURCHASE_ORDER_LIFECYCLE.assert_transition(
            PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.SENT
        )


def test_goods_cannot_be_received_against_an_order_the_vendor_has_not_seen():
    for status in (
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
    ):
        with pytest.raises(InvalidStateTransitionError):
            PURCHASE_ORDER_LIFECYCLE.assert_transition(
                status, PurchaseOrderStatus.PARTIALLY_RECEIVED
            )


def test_a_sent_order_can_be_received_without_an_acknowledgement():
    """Documented assumption: goods that arrive can be taken in.

    Acknowledgement is a courtesy from the vendor. Gating the store on it would
    mean a lorry at the gate and a document that refuses to accept the delivery.
    """
    PURCHASE_ORDER_LIFECYCLE.assert_transition(
        PurchaseOrderStatus.SENT, PurchaseOrderStatus.PARTIALLY_RECEIVED
    )
    PURCHASE_ORDER_LIFECYCLE.assert_transition(
        PurchaseOrderStatus.SENT, PurchaseOrderStatus.RECEIVED
    )


def test_an_order_with_no_inspection_completes_without_passing_through_qc():
    """Documented assumption: a QC state nobody inspects is a meaningless state."""
    PURCHASE_ORDER_LIFECYCLE.assert_transition(
        PurchaseOrderStatus.RECEIVED, PurchaseOrderStatus.GRN_COMPLETED
    )


# --- cancellation is bounded by physical movement -----------------------------


@pytest.mark.parametrize(
    "status",
    [
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.ACKNOWLEDGED,
    ],
)
def test_an_order_can_be_cancelled_until_something_is_received(status):
    PURCHASE_ORDER_LIFECYCLE.assert_transition(status, PurchaseOrderStatus.CANCELLED)


@pytest.mark.parametrize(
    "status",
    [
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.QC_PENDING,
        PurchaseOrderStatus.GRN_COMPLETED,
        PurchaseOrderStatus.INVOICED,
        PurchaseOrderStatus.PAID,
    ],
)
def test_an_order_that_moved_stock_can_never_be_cancelled(status):
    """The ledger would describe a receipt against a document claiming not to exist."""
    with pytest.raises(InvalidStateTransitionError):
        PURCHASE_ORDER_LIFECYCLE.assert_transition(status, PurchaseOrderStatus.CANCELLED)


def test_a_part_delivered_order_is_short_closed_instead_of_cancelled():
    PURCHASE_ORDER_LIFECYCLE.assert_transition(
        PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.CLOSED
    )


def test_rejection_is_only_available_to_the_approver():
    PURCHASE_ORDER_LIFECYCLE.assert_transition(
        PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.REJECTED
    )
    with pytest.raises(InvalidStateTransitionError):
        PURCHASE_ORDER_LIFECYCLE.assert_transition(
            PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.REJECTED
        )


@pytest.mark.parametrize(
    "status",
    [PurchaseOrderStatus.CLOSED, PurchaseOrderStatus.CANCELLED, PurchaseOrderStatus.REJECTED],
)
def test_terminal_states_have_no_way_out(status):
    assert PURCHASE_ORDER_LIFECYCLE.is_terminal(status)
    assert PURCHASE_ORDER_LIFECYCLE.allowed_targets(status) == []


# --- the permissions each transition demands ----------------------------------


def test_approval_and_receiving_demand_different_permissions():
    """Raising an order and approving it are deliberately separate authorities."""
    approve = PURCHASE_ORDER_LIFECYCLE.find(
        PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.APPROVED
    )
    receive = PURCHASE_ORDER_LIFECYCLE.find(
        PurchaseOrderStatus.ACKNOWLEDGED, PurchaseOrderStatus.RECEIVED
    )
    assert approve is not None and approve.permission is Permission.PURCHASE_ORDER_APPROVE
    assert receive is not None and receive.permission is Permission.GRN_POST


def test_every_purchase_order_transition_declares_a_permission():
    """An unguarded transition is one anybody who can read the document can make."""
    unguarded = [
        (str(t.source), str(t.target))
        for t in PURCHASE_ORDER_LIFECYCLE.transitions
        if t.permission is None
    ]
    assert unguarded == []


def test_transitions_that_move_stock_or_money_are_marked_irreversible():
    """The UI demands confirmation on these; mislabelling one loses that guard."""
    approve = PURCHASE_ORDER_LIFECYCLE.find(
        PurchaseOrderStatus.PENDING_APPROVAL, PurchaseOrderStatus.APPROVED
    )
    first_receipt = PURCHASE_ORDER_LIFECYCLE.find(
        PurchaseOrderStatus.ACKNOWLEDGED, PurchaseOrderStatus.PARTIALLY_RECEIVED
    )
    assert approve is not None and approve.is_irreversible
    assert first_receipt is not None and first_receipt.is_irreversible


def test_available_actions_are_filtered_by_what_the_actor_holds():
    """Drives the UI buttons; the backend still re-checks on the request itself."""
    buyer = [Permission.PURCHASE_ORDER_UPDATE, Permission.PURCHASE_ORDER_CANCEL]
    actions = PURCHASE_ORDER_LIFECYCLE.available_actions(PurchaseOrderStatus.DRAFT, buyer)
    assert {t.target for t in actions} == {
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.CANCELLED,
    }

    approver_only = [Permission.PURCHASE_ORDER_APPROVE]
    assert (
        PURCHASE_ORDER_LIFECYCLE.available_actions(PurchaseOrderStatus.DRAFT, approver_only) == []
    )


# -----------------------------------------------------------------------------
# Goods receipt and purchase return
# -----------------------------------------------------------------------------


def test_a_posted_receipt_is_terminal():
    """A posted receipt wrote the ledger; it is reversed, never retracted."""
    assert GOODS_RECEIPT_LIFECYCLE.is_terminal(GoodsReceiptStatus.POSTED)
    with pytest.raises(InvalidStateTransitionError):
        GOODS_RECEIPT_LIFECYCLE.assert_transition(
            GoodsReceiptStatus.POSTED, GoodsReceiptStatus.CANCELLED
        )


def test_only_a_draft_receipt_can_be_cancelled():
    GOODS_RECEIPT_LIFECYCLE.assert_transition(
        GoodsReceiptStatus.DRAFT, GoodsReceiptStatus.CANCELLED
    )


def test_posting_a_receipt_requires_the_posting_permission():
    post = GOODS_RECEIPT_LIFECYCLE.find(GoodsReceiptStatus.DRAFT, GoodsReceiptStatus.POSTED)
    assert post is not None
    assert post.permission is Permission.GRN_POST
    assert post.is_irreversible


def test_a_posted_return_is_terminal_too():
    assert PURCHASE_RETURN_LIFECYCLE.is_terminal(PurchaseReturnStatus.POSTED)
    with pytest.raises(InvalidStateTransitionError):
        PURCHASE_RETURN_LIFECYCLE.assert_transition(
            PurchaseReturnStatus.POSTED, PurchaseReturnStatus.CANCELLED
        )


# -----------------------------------------------------------------------------
# Requisition, RFQ and quotation
# -----------------------------------------------------------------------------


def test_a_requisition_must_be_approved_before_it_can_be_converted():
    with pytest.raises(InvalidStateTransitionError):
        REQUISITION_LIFECYCLE.assert_transition(
            RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.CONVERTED
        )
    REQUISITION_LIFECYCLE.assert_transition(
        RequisitionStatus.APPROVED, RequisitionStatus.CONVERTED
    )


def test_a_rejected_requisition_is_terminal():
    """Fixing a rejected document means raising a new one, with a new number."""
    assert REQUISITION_LIFECYCLE.is_terminal(RequisitionStatus.REJECTED)


def test_approving_and_raising_a_requisition_are_different_permissions():
    submit = REQUISITION_LIFECYCLE.find(
        RequisitionStatus.DRAFT, RequisitionStatus.PENDING_APPROVAL
    )
    approve = REQUISITION_LIFECYCLE.find(
        RequisitionStatus.PENDING_APPROVAL, RequisitionStatus.APPROVED
    )
    assert submit is not None and submit.permission is Permission.PURCHASE_REQUISITION_CREATE
    assert approve is not None and approve.permission is Permission.PURCHASE_REQUISITION_APPROVE


def test_a_closed_rfq_takes_no_further_action():
    assert RFQ_LIFECYCLE.is_terminal(RfqStatus.CLOSED)
    with pytest.raises(InvalidStateTransitionError):
        RFQ_LIFECYCLE.assert_transition(RfqStatus.CLOSED, RfqStatus.SENT)


def test_an_unsent_rfq_cannot_be_closed():
    """Closing an RFQ nobody was asked to quote on says nothing."""
    with pytest.raises(InvalidStateTransitionError):
        RFQ_LIFECYCLE.assert_transition(RfqStatus.DRAFT, RfqStatus.CLOSED)


def test_a_decided_quotation_cannot_be_decided_again():
    for terminal in (QuotationStatus.SELECTED, QuotationStatus.REJECTED, QuotationStatus.EXPIRED):
        assert QUOTATION_LIFECYCLE.is_terminal(terminal)
        with pytest.raises(InvalidStateTransitionError):
            QUOTATION_LIFECYCLE.assert_transition(terminal, QuotationStatus.SELECTED)


def test_a_quotation_can_be_selected_without_a_formal_review_step():
    """Two quotations and an obvious winner does not need a review state first."""
    QUOTATION_LIFECYCLE.assert_transition(QuotationStatus.RECEIVED, QuotationStatus.SELECTED)
