"""Bin moves, scrap disposals and internal issues - the rules that need no database.

Three groups:

* the documents' state machines are well formed, and offer exactly the edges
  the screens draw buttons for;
* the request schemas refuse a document that could never post - a bin move to
  the bin it came from, a write-off that realised money, an issue to nobody;
* the engine's scrap-queue arithmetic: which movements draw on the scrap pool,
  and what a movement into scrap leaves in the queue a disposal is later
  valued from.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError as SchemaError

from src.core.permissions import Permission
from src.modules.inventory.models import (
    BinMoveStatus,
    InternalIssueStatus,
    ScrapDisposalKind,
    ScrapDisposalStatus,
    StockState,
    TransactionType,
)
from src.modules.inventory.schemas import (
    MAX_DOCUMENT_LINES,
    BinMoveCreate,
    InternalIssueCreate,
    RejectionRequest,
    ScrapDisposalCreate,
)
from src.modules.inventory.service import (
    BIN_MOVE_FLOW,
    INTERNAL_ISSUE_FLOW,
    SCRAP_DISPOSAL_FLOW,
    SCRAP_DRAWING_TYPES,
    SCRAP_LANDING_TYPES,
    TARGET_QUALIFIED_TYPES,
    LayerCost,
    LayerView,
    MovementRequest,
    draws_on_scrap,
    scrap_layers_for,
    value_movement,
)

pytestmark = pytest.mark.unit

ITEM = uuid.uuid4()
WAREHOUSE = uuid.uuid4()
BIN_A = uuid.uuid4()
BIN_B = uuid.uuid4()


# -----------------------------------------------------------------------------
# State machines
# -----------------------------------------------------------------------------


class TestTheMachines:
    @pytest.mark.parametrize("machine", [BIN_MOVE_FLOW, SCRAP_DISPOSAL_FLOW, INTERNAL_ISSUE_FLOW])
    def test_each_machine_is_well_formed(self, machine: object) -> None:
        machine.validate()  # type: ignore[attr-defined]

    def test_a_bin_move_only_posts(self) -> None:
        assert BIN_MOVE_FLOW.allowed_targets(BinMoveStatus.DRAFT) == [BinMoveStatus.POSTED]
        assert BIN_MOVE_FLOW.is_terminal(BinMoveStatus.POSTED)
        assert (
            BIN_MOVE_FLOW.required_permission(BinMoveStatus.DRAFT, BinMoveStatus.POSTED)
            is Permission.INVENTORY_TRANSFER
        )

    def test_a_scrap_disposal_only_posts(self) -> None:
        assert SCRAP_DISPOSAL_FLOW.allowed_targets(ScrapDisposalStatus.DRAFT) == [
            ScrapDisposalStatus.POSTED
        ]
        assert (
            SCRAP_DISPOSAL_FLOW.required_permission(
                ScrapDisposalStatus.DRAFT, ScrapDisposalStatus.POSTED
            )
            is Permission.INVENTORY_SCRAP_DISPOSE
        )

    def test_a_draft_issue_posts_goes_for_approval_or_is_cancelled(self) -> None:
        assert set(INTERNAL_ISSUE_FLOW.allowed_targets(InternalIssueStatus.DRAFT)) == {
            InternalIssueStatus.POSTED,
            InternalIssueStatus.PENDING_APPROVAL,
            InternalIssueStatus.CANCELLED,
        }

    def test_a_pending_issue_is_approved_into_posted_or_cancelled(self) -> None:
        assert set(INTERNAL_ISSUE_FLOW.allowed_targets(InternalIssueStatus.PENDING_APPROVAL)) == {
            InternalIssueStatus.POSTED,
            InternalIssueStatus.CANCELLED,
        }
        assert (
            INTERNAL_ISSUE_FLOW.required_permission(
                InternalIssueStatus.PENDING_APPROVAL, InternalIssueStatus.POSTED
            )
            is Permission.INTERNAL_ISSUE_APPROVE
        )

    @pytest.mark.parametrize(
        "terminal", [InternalIssueStatus.POSTED, InternalIssueStatus.CANCELLED]
    )
    def test_posted_and_cancelled_issues_are_final(self, terminal: InternalIssueStatus) -> None:
        assert INTERNAL_ISSUE_FLOW.is_terminal(terminal)
        assert INTERNAL_ISSUE_FLOW.allowed_targets(terminal) == []

    def test_a_posted_issue_cannot_be_cancelled(self) -> None:
        assert not INTERNAL_ISSUE_FLOW.can_transition(
            InternalIssueStatus.POSTED, InternalIssueStatus.CANCELLED
        )

    def test_there_is_no_way_back_from_approval_to_a_draft(self) -> None:
        assert not INTERNAL_ISSUE_FLOW.can_transition(
            InternalIssueStatus.PENDING_APPROVAL, InternalIssueStatus.DRAFT
        )


# -----------------------------------------------------------------------------
# Bin move requests
# -----------------------------------------------------------------------------


def _bin_move(**line: object) -> dict[str, object]:
    return {
        "warehouse_id": str(WAREHOUSE),
        "reason": "Put received goods away",
        "lines": [{"item_id": str(ITEM), "quantity": "5", **line}],
    }


class TestBinMoveRequests:
    def test_a_putaway_from_the_unbinned_position_into_a_bin_is_accepted(self) -> None:
        move = BinMoveCreate.model_validate(_bin_move(to_location_id=str(BIN_A)))
        assert move.lines[0].from_location_id is None
        assert move.lines[0].to_location_id == BIN_A

    def test_a_move_from_a_bin_back_to_the_unbinned_position_is_accepted(self) -> None:
        move = BinMoveCreate.model_validate(_bin_move(from_location_id=str(BIN_A)))
        assert move.lines[0].to_location_id is None

    def test_the_same_bin_on_both_ends_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="must differ"):
            BinMoveCreate.model_validate(
                _bin_move(from_location_id=str(BIN_A), to_location_id=str(BIN_A))
            )

    def test_unbinned_to_unbinned_is_refused(self) -> None:
        """Same warehouse, no bin on either end: a movement that moves nothing."""
        with pytest.raises(SchemaError, match="must differ"):
            BinMoveCreate.model_validate(_bin_move())

    def test_two_lines_from_one_source_position_are_refused(self) -> None:
        payload = _bin_move(to_location_id=str(BIN_A))
        payload["lines"] = [
            {"item_id": str(ITEM), "quantity": "2", "to_location_id": str(BIN_A)},
            {"item_id": str(ITEM), "quantity": "3", "to_location_id": str(BIN_B)},
        ]
        with pytest.raises(SchemaError, match="source bin appears on two lines"):
            BinMoveCreate.model_validate(payload)

    def test_the_line_limit_holds(self) -> None:
        payload = _bin_move()
        payload["lines"] = [
            {"item_id": str(uuid.uuid4()), "quantity": "1", "to_location_id": str(BIN_A)}
            for _ in range(MAX_DOCUMENT_LINES + 1)
        ]
        with pytest.raises(SchemaError, match="may not exceed"):
            BinMoveCreate.model_validate(payload)

    @pytest.mark.parametrize("quantity", ["0", "-1"])
    def test_a_quantity_must_be_positive(self, quantity: str) -> None:
        with pytest.raises(SchemaError):
            BinMoveCreate.model_validate(_bin_move(to_location_id=str(BIN_A), quantity=quantity))

    def test_a_field_the_document_does_not_have_is_refused(self) -> None:
        with pytest.raises(SchemaError):
            BinMoveCreate.model_validate(_bin_move(to_location_id=str(BIN_A), unit_cost="5"))


# -----------------------------------------------------------------------------
# Scrap disposal requests
# -----------------------------------------------------------------------------


def _disposal(**header: object) -> dict[str, object]:
    return {
        "warehouse_id": str(WAREHOUSE),
        "disposal_kind": "SALE",
        "reason": "Monthly sale of brass offcuts",
        "lines": [{"item_id": str(ITEM), "quantity": "4"}],
        **header,
    }


class TestScrapDisposalRequests:
    def test_a_sale_records_what_it_realised(self) -> None:
        disposal = ScrapDisposalCreate.model_validate(
            _disposal(realised_amount="1560.00", buyer_reference="  Shree   Metals ")
        )
        assert disposal.realised_amount == Decimal("1560.00")
        assert disposal.buyer_reference == "Shree Metals"

    def test_a_write_off_realises_nothing(self) -> None:
        with pytest.raises(SchemaError, match="write-off realises nothing"):
            ScrapDisposalCreate.model_validate(
                _disposal(disposal_kind="WRITE_OFF", realised_amount="10")
            )

    def test_a_write_off_with_nothing_realised_is_accepted(self) -> None:
        disposal = ScrapDisposalCreate.model_validate(_disposal(disposal_kind="WRITE_OFF"))
        assert disposal.disposal_kind is ScrapDisposalKind.WRITE_OFF
        assert disposal.realised_amount == 0

    def test_a_negative_amount_is_refused(self) -> None:
        with pytest.raises(SchemaError):
            ScrapDisposalCreate.model_validate(_disposal(realised_amount="-1"))

    def test_an_unknown_kind_is_refused(self) -> None:
        with pytest.raises(SchemaError):
            ScrapDisposalCreate.model_validate(_disposal(disposal_kind="DONATION"))

    def test_two_lines_for_one_position_are_refused(self) -> None:
        payload = _disposal()
        payload["lines"] = [
            {"item_id": str(ITEM), "quantity": "1"},
            {"item_id": str(ITEM), "quantity": "2"},
        ]
        with pytest.raises(SchemaError, match="appears on two lines"):
            ScrapDisposalCreate.model_validate(payload)


# -----------------------------------------------------------------------------
# Internal issue requests
# -----------------------------------------------------------------------------


def _issue(**header: object) -> dict[str, object]:
    return {
        "warehouse_id": str(WAREHOUSE),
        "reason": "Weekly consumables for the polishing line",
        "lines": [{"item_id": str(ITEM), "quantity": "12"}],
        **header,
    }


class TestInternalIssueRequests:
    def test_an_issue_to_nobody_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="department or the cost centre"):
            InternalIssueCreate.model_validate(_issue())

    @pytest.mark.parametrize("recipient", ["department_id", "cost_centre_id"])
    def test_either_recipient_alone_is_enough(self, recipient: str) -> None:
        issue = InternalIssueCreate.model_validate(_issue(**{recipient: str(uuid.uuid4())}))
        assert getattr(issue, recipient) is not None

    def test_both_recipients_together_are_accepted(self) -> None:
        issue = InternalIssueCreate.model_validate(
            _issue(department_id=str(uuid.uuid4()), cost_centre_id=str(uuid.uuid4()))
        )
        assert issue.department_id is not None and issue.cost_centre_id is not None


class TestRejectionRequests:
    @pytest.mark.parametrize("note", ["", "  ", " a "])
    def test_a_rejection_must_say_why(self, note: str) -> None:
        with pytest.raises(SchemaError):
            RejectionRequest.model_validate({"note": note})

    def test_the_reason_is_tidied(self) -> None:
        assert (
            RejectionRequest.model_validate({"note": "  Count   needs a recount "}).note
            == "Count needs a recount"
        )


# -----------------------------------------------------------------------------
# The scrap queue
# -----------------------------------------------------------------------------


def _movement(transaction_type: TransactionType, **overrides: object) -> MovementRequest:
    fields: dict[str, object] = {
        "item_id": ITEM,
        "warehouse_id": WAREHOUSE,
        "quantity": Decimal("3"),
        "transaction_type": transaction_type,
        "reference_type": "test",
        "reason": "test",
    }
    fields.update(overrides)
    return MovementRequest(**fields)  # type: ignore[arg-type]


class TestWhichMovementsDrawOnScrap:
    def test_a_disposal_does(self) -> None:
        assert draws_on_scrap(_movement(TransactionType.SCRAP_DISPOSAL))
        assert draws_on_scrap(
            _movement(TransactionType.SCRAP_DISPOSAL, stock_state=StockState.SCRAP)
        )

    def test_scrapping_does_not(self) -> None:
        assert not draws_on_scrap(_movement(TransactionType.SCRAP))
        assert not draws_on_scrap(
            _movement(
                TransactionType.SCRAP,
                from_state=StockState.QC_HOLD,
                stock_state=StockState.SCRAP,
            )
        )

    def test_an_ordinary_issue_does_not(self) -> None:
        assert not draws_on_scrap(_movement(TransactionType.MATERIAL_ISSUE))

    def test_the_type_sets_follow_the_transition_table(self) -> None:
        assert frozenset({TransactionType.SCRAP}) == SCRAP_LANDING_TYPES
        assert frozenset({TransactionType.SCRAP_DISPOSAL}) == SCRAP_DRAWING_TYPES
        # A receipt names the pool it lands in, an issue the pool it leaves.
        assert TransactionType.PURCHASE_RECEIPT in TARGET_QUALIFIED_TYPES
        assert TransactionType.SCRAP in TARGET_QUALIFIED_TYPES
        assert TransactionType.MATERIAL_ISSUE not in TARGET_QUALIFIED_TYPES
        assert TransactionType.SCRAP_DISPOSAL not in TARGET_QUALIFIED_TYPES


class TestWhatScrapRemembers:
    """A movement into scrap opens scrap layers at exactly what it charged."""

    def test_scrapping_on_hand_stock_keeps_each_layer_it_drew(self) -> None:
        valuation = value_movement(
            on_hand_after=Decimal("15"),
            magnitude=Decimal("15"),
            on_hand_delta=Decimal("-15"),
            layers=[
                LayerView(remaining=Decimal("10"), unit_cost=Decimal("100")),
                LayerView(remaining=Decimal("20"), unit_cost=Decimal("120")),
            ],
            average_before=Decimal("113.333333"),
            movement_unit_cost=None,
            fallback_cost=Decimal("90"),
        )
        assert scrap_layers_for(valuation, Decimal("15")) == (
            LayerCost(quantity=Decimal("10.0000"), unit_cost=Decimal("100.000000")),
            LayerCost(quantity=Decimal("5.0000"), unit_cost=Decimal("120.000000")),
        )
        assert valuation.total_cost == Decimal("1600.00")

    def test_an_uncovered_remainder_is_kept_at_the_price_it_was_charged(self) -> None:
        valuation = value_movement(
            on_hand_after=Decimal("0"),
            magnitude=Decimal("8"),
            on_hand_delta=Decimal("-8"),
            layers=[LayerView(remaining=Decimal("5"), unit_cost=Decimal("100"))],
            average_before=Decimal("100"),
            movement_unit_cost=None,
            fallback_cost=Decimal("90"),
        )
        remembered = scrap_layers_for(valuation, Decimal("8"))
        assert remembered == (
            LayerCost(quantity=Decimal("5.0000"), unit_cost=Decimal("100.000000")),
            LayerCost(quantity=Decimal("3.0000"), unit_cost=valuation.uncovered_unit_cost),
        )
        assert sum(cost.quantity for cost in remembered) == Decimal("8")

    def test_scrap_at_a_vendor_is_kept_at_the_one_price_it_was_charged(self) -> None:
        """No on-hand moves, so no layer is drawn - the ledger row's price is all there is."""
        valuation = value_movement(
            on_hand_after=Decimal("40"),
            magnitude=Decimal("6"),
            on_hand_delta=Decimal("0"),
            layers=[LayerView(remaining=Decimal("40"), unit_cost=Decimal("75"))],
            average_before=Decimal("75"),
            movement_unit_cost=None,
            fallback_cost=Decimal("60"),
        )
        assert scrap_layers_for(valuation, Decimal("6")) == (
            LayerCost(quantity=Decimal("6.0000"), unit_cost=valuation.unit_cost),
        )
        assert valuation.unit_cost == Decimal("75.000000")
