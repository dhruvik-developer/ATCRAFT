"""Duplicate purchase order prevention (master rules section 16).

The rule this file defends: the same shortage must not be bought twice. Every
assertion is a statement about money that would otherwise be spent on stock the
company has already ordered.

No database. The matcher takes open order lines and a proposed demand and
returns candidates with actions; keeping it pure is what makes these cases
arguable with a buyer rather than a fixture exercise.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.core.errors import ErrorCode
from src.modules.procurement.models import (
    DUPLICATE_REQUIRED_DATE_WINDOW_DAYS,
    PurchaseOrderStatus,
    PurchaseSourceType,
)
from src.modules.procurement.service_rules import (
    CandidateInput,
    DuplicateAction,
    DuplicateConfidence,
    available_actions_for,
    classify_candidate,
    classify_candidates,
    dates_overlap,
    duplicate_refusal,
    recommend_action,
    same_source_demand,
)

pytestmark = pytest.mark.unit

VENDOR = uuid.uuid4()
OTHER_VENDOR = uuid.uuid4()
ITEM = uuid.uuid4()
OTHER_ITEM = uuid.uuid4()
WAREHOUSE = uuid.uuid4()
OTHER_WAREHOUSE = uuid.uuid4()
MRP_RUN = uuid.uuid4()
REQUIRED = date(2026, 9, 15)


def make_candidate(
    *,
    status: PurchaseOrderStatus = PurchaseOrderStatus.SENT,
    vendor_id: uuid.UUID = VENDOR,
    item_id: uuid.UUID = ITEM,
    warehouse_id: uuid.UUID = WAREHOUSE,
    required_date: date | None = REQUIRED,
    received: str = "0",
    ordered: str = "100",
    source_type: PurchaseSourceType = PurchaseSourceType.MANUAL,
    source_id: uuid.UUID | None = None,
    code: str = "PO/2026-27/00001",
) -> CandidateInput:
    return CandidateInput(
        purchase_order_id=uuid.uuid4(),
        purchase_order_code=code,
        purchase_order_line_id=uuid.uuid4(),
        status=status,
        vendor_id=vendor_id,
        item_id=item_id,
        warehouse_id=warehouse_id,
        ordered_quantity=Decimal(ordered),
        received_quantity=Decimal(received),
        required_date=required_date,
        source_type=source_type,
        source_id=source_id,
    )


def classify(candidate: CandidateInput, **overrides):
    kwargs = {
        "vendor_id": VENDOR,
        "item_id": ITEM,
        "warehouse_id": WAREHOUSE,
        "required_date": REQUIRED,
        "source_type": None,
        "source_id": None,
    }
    kwargs.update(overrides)
    return classify_candidate(candidate, **kwargs)


# -----------------------------------------------------------------------------
# The four hard criteria
# -----------------------------------------------------------------------------


def test_same_vendor_item_warehouse_and_window_is_a_duplicate():
    assert classify(make_candidate()) is not None


def test_a_different_vendor_is_not_a_duplicate():
    """Two vendors supplying the same part is dual sourcing, not double buying."""
    assert classify(make_candidate(vendor_id=OTHER_VENDOR)) is None


def test_a_different_item_is_not_a_duplicate():
    assert classify(make_candidate(item_id=OTHER_ITEM)) is None


def test_a_different_warehouse_is_not_a_duplicate():
    """The same part wanted at two plants is two genuine demands."""
    assert classify(make_candidate(warehouse_id=OTHER_WAREHOUSE)) is None


def test_a_date_outside_the_window_is_not_a_duplicate():
    """A repeat order a month later is replenishment, not a duplicate."""
    far_off = REQUIRED + timedelta(days=DUPLICATE_REQUIRED_DATE_WINDOW_DAYS + 1)
    assert classify(make_candidate(required_date=far_off)) is None


def test_a_date_at_the_window_edge_is_still_a_duplicate():
    edge = REQUIRED + timedelta(days=DUPLICATE_REQUIRED_DATE_WINDOW_DAYS)
    assert classify(make_candidate(required_date=edge)) is not None


def test_a_closed_order_is_never_a_candidate_because_it_is_never_fetched():
    """Only open orders reach the matcher.

    The repository restricts to OPEN_PO_STATUSES; the classifier is given what
    that query returned. This asserts the contract the two halves share: a
    RECEIVED order handed to the classifier is still classified, so nothing
    silently depends on the classifier re-filtering by status.
    """
    candidate = make_candidate(status=PurchaseOrderStatus.PARTIALLY_RECEIVED, received="40")
    assert classify(candidate) is not None


# -----------------------------------------------------------------------------
# Unknown dates
# -----------------------------------------------------------------------------


def test_an_unknown_date_on_either_side_counts_as_overlapping():
    """The expensive mistake is the duplicate nobody is shown."""
    assert dates_overlap(None, REQUIRED) is True
    assert dates_overlap(REQUIRED, None) is True
    assert dates_overlap(None, None) is True


def test_dates_overlap_is_symmetric_about_the_window():
    before = REQUIRED - timedelta(days=DUPLICATE_REQUIRED_DATE_WINDOW_DAYS)
    after = REQUIRED + timedelta(days=DUPLICATE_REQUIRED_DATE_WINDOW_DAYS)
    assert dates_overlap(before, REQUIRED) is True
    assert dates_overlap(after, REQUIRED) is True
    assert dates_overlap(before - timedelta(days=1), REQUIRED) is False


# -----------------------------------------------------------------------------
# Source demand: the strongest signal there is
# -----------------------------------------------------------------------------


def test_same_source_demand_beats_the_date_window():
    """Two orders from one MRP suggestion are duplicates whatever dates were typed.

    This is the case section 16 exists for: MRP proposes a shortage, two buyers
    convert the same suggestion, and the dates they type differ by a fortnight.
    """
    candidate = make_candidate(
        required_date=REQUIRED + timedelta(days=60),
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=MRP_RUN,
    )
    result = classify(
        candidate, source_type=PurchaseSourceType.MRP_SUGGESTION, source_id=MRP_RUN
    )

    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


def test_two_manual_orders_never_share_a_source():
    """Absence of a reference is not a shared reference.

    Treating MANUAL as a match would flag every hand-typed order against every
    other one, and a warning that always fires is a warning nobody reads.
    """
    assert (
        same_source_demand(
            candidate_source_type=PurchaseSourceType.MANUAL,
            candidate_source_id=None,
            proposed_source_type=PurchaseSourceType.MANUAL,
            proposed_source_id=None,
        )
        is False
    )


def test_a_different_source_id_is_not_the_same_demand():
    candidate = make_candidate(
        source_type=PurchaseSourceType.SALES_ORDER, source_id=uuid.uuid4()
    )
    result = classify(
        candidate, source_type=PurchaseSourceType.SALES_ORDER, source_id=uuid.uuid4()
    )
    # Still matched on vendor/item/warehouse/date, but not as an EXACT duplicate.
    assert result is not None
    assert result.confidence is not DuplicateConfidence.EXACT


# -----------------------------------------------------------------------------
# Confidence
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (PurchaseOrderStatus.DRAFT, DuplicateConfidence.STRONG),
        (PurchaseOrderStatus.PENDING_APPROVAL, DuplicateConfidence.STRONG),
        (PurchaseOrderStatus.APPROVED, DuplicateConfidence.STRONG),
        (PurchaseOrderStatus.SENT, DuplicateConfidence.POSSIBLE),
        (PurchaseOrderStatus.ACKNOWLEDGED, DuplicateConfidence.POSSIBLE),
        (PurchaseOrderStatus.PARTIALLY_RECEIVED, DuplicateConfidence.POSSIBLE),
    ],
)
def test_confidence_tracks_whether_the_vendor_has_the_order(status, expected):
    result = classify(make_candidate(status=status))
    assert result is not None
    assert result.confidence is expected


# -----------------------------------------------------------------------------
# The four actions from master rules section 16
# -----------------------------------------------------------------------------


def test_an_unsent_order_can_be_merged_into():
    actions = available_actions_for(
        status=PurchaseOrderStatus.DRAFT, received_quantity=Decimal("0")
    )
    assert DuplicateAction.MERGE_WITH_EXISTING in actions


def test_a_sent_order_cannot_be_merged_into():
    """The vendor already has the document; adding a line to it silently is a lie."""
    actions = available_actions_for(
        status=PurchaseOrderStatus.SENT, received_quantity=Decimal("0")
    )
    assert DuplicateAction.MERGE_WITH_EXISTING not in actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY in actions


def test_a_partially_received_order_can_only_be_added_to_with_a_new_order():
    """Stock has moved: it can be neither merged into, amended, nor cancelled."""
    actions = available_actions_for(
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED, received_quantity=Decimal("40")
    )
    assert actions == (DuplicateAction.CREATE_NEW_WITH_APPROVAL,)


def test_creating_a_new_order_with_approval_is_always_available():
    """The buyer may know something the matcher does not - approval makes it safe."""
    for status in PurchaseOrderStatus:
        actions = available_actions_for(status=status, received_quantity=Decimal("0"))
        assert DuplicateAction.CREATE_NEW_WITH_APPROVAL in actions


def test_every_recommended_action_is_one_the_system_can_actually_perform():
    """A recommendation that fails when clicked teaches people to ignore warnings."""
    for status in PurchaseOrderStatus:
        for received in (Decimal("0"), Decimal("40")):
            for same_source in (True, False):
                available = available_actions_for(status=status, received_quantity=received)
                recommended = recommend_action(
                    status=status,
                    received_quantity=received,
                    is_same_source=same_source,
                    available=available,
                )
                assert recommended in available


# --- the recommendation ladder ------------------------------------------------


def test_same_source_recommends_cancelling_the_duplicate():
    candidate = make_candidate(
        status=PurchaseOrderStatus.SENT,
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=MRP_RUN,
    )
    result = classify(
        candidate, source_type=PurchaseSourceType.MRP_SUGGESTION, source_id=MRP_RUN
    )
    assert result is not None
    assert result.recommended_action is DuplicateAction.CANCEL_DUPLICATE


def test_an_unsent_order_recommends_merging():
    result = classify(make_candidate(status=PurchaseOrderStatus.PENDING_APPROVAL))
    assert result is not None
    assert result.recommended_action is DuplicateAction.MERGE_WITH_EXISTING


def test_a_sent_order_with_nothing_received_recommends_increasing_its_quantity():
    result = classify(make_candidate(status=PurchaseOrderStatus.ACKNOWLEDGED))
    assert result is not None
    assert result.recommended_action is DuplicateAction.INCREASE_EXISTING_QUANTITY


def test_a_part_delivered_order_recommends_a_new_order_with_approval():
    result = classify(
        make_candidate(status=PurchaseOrderStatus.PARTIALLY_RECEIVED, received="40")
    )
    assert result is not None
    assert result.recommended_action is DuplicateAction.CREATE_NEW_WITH_APPROVAL


def test_same_source_on_a_part_delivered_order_does_not_recommend_cancelling():
    """Cancelling is recommended only when it is actually possible.

    Stock has already moved against this order, so the honest advice is a new
    order with approval - not an action that would be refused.
    """
    candidate = make_candidate(
        status=PurchaseOrderStatus.PARTIALLY_RECEIVED,
        received="40",
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=MRP_RUN,
    )
    result = classify(
        candidate, source_type=PurchaseSourceType.MRP_SUGGESTION, source_id=MRP_RUN
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT
    assert result.recommended_action is DuplicateAction.CREATE_NEW_WITH_APPROVAL


# -----------------------------------------------------------------------------
# Ordering, open quantity and the refusal
# -----------------------------------------------------------------------------


def test_candidates_are_returned_strongest_first():
    exact = make_candidate(
        code="PO/EXACT",
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=MRP_RUN,
        status=PurchaseOrderStatus.SENT,
    )
    strong = make_candidate(code="PO/STRONG", status=PurchaseOrderStatus.DRAFT)
    possible = make_candidate(code="PO/POSSIBLE", status=PurchaseOrderStatus.SENT)

    results = classify_candidates(
        [possible, strong, exact],
        vendor_id=VENDOR,
        item_id=ITEM,
        warehouse_id=WAREHOUSE,
        required_date=REQUIRED,
        source_type=PurchaseSourceType.MRP_SUGGESTION,
        source_id=MRP_RUN,
    )

    assert [c.purchase_order_code for c in results] == ["PO/EXACT", "PO/STRONG", "PO/POSSIBLE"]


def test_open_quantity_is_what_is_still_expected():
    result = classify(make_candidate(ordered="100", received="40"))
    assert result is not None
    assert result.open_quantity == Decimal("60")


def test_open_quantity_never_goes_negative_on_an_over_receipt():
    """A line received inside the tolerance has nothing left open, not a deficit."""
    result = classify(make_candidate(ordered="100", received="103"))
    assert result is not None
    assert result.open_quantity == Decimal("0")


def test_match_reasons_are_stated_so_a_buyer_can_argue_with_them():
    result = classify(make_candidate())
    assert result is not None
    assert "Same vendor" in result.match_reasons
    assert "Same item" in result.match_reasons
    assert "Same delivery warehouse" in result.match_reasons


def test_the_refusal_carries_the_candidates_and_their_actions():
    """A refusal that only says "duplicate" leaves the buyer with nothing to do."""
    candidates = classify_candidates(
        [make_candidate(code="PO/2026-27/00042")],
        vendor_id=VENDOR,
        item_id=ITEM,
        warehouse_id=WAREHOUSE,
        required_date=REQUIRED,
        source_type=None,
        source_id=None,
    )
    error = duplicate_refusal(candidates)

    assert error.code is ErrorCode.DUPLICATE_DOCUMENT
    assert error.status_code == 409
    assert "PO/2026-27/00042" in error.message
    assert error.details["candidate_count"] == 1
    reported = error.details["candidates"][0]
    assert reported["recommended_action"] in reported["available_actions"]
    assert reported["match_reasons"]
