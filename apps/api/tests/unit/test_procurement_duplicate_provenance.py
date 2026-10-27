"""Demand identity, procurement categories and receipt-safe duplicate actions."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.core.demand_provenance import DemandReference
from src.modules.procurement.models import PurchaseOrderStatus, PurchaseSourceType
from src.modules.procurement.service_rules import (
    CandidateInput,
    DuplicateAction,
    DuplicateConfidence,
    available_actions_for,
    classify_candidate,
    classify_candidates,
    demand_ref_keys,
    duplicate_refusal,
)

pytestmark = pytest.mark.unit

VENDOR = uuid.uuid4()
ITEM = uuid.uuid4()
OTHER_ITEM = uuid.uuid4()
WAREHOUSE = uuid.uuid4()
CATEGORY = uuid.uuid4()
REQUIRED = date(2026, 10, 27)


def make_candidate(**overrides: object) -> CandidateInput:
    candidate = CandidateInput(
        purchase_order_id=uuid.uuid4(),
        purchase_order_code="PO/2026-27/00001",
        purchase_order_line_id=uuid.uuid4(),
        status=PurchaseOrderStatus.DRAFT,
        vendor_id=VENDOR,
        item_id=ITEM,
        warehouse_id=WAREHOUSE,
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("0"),
        required_date=REQUIRED,
        source_type=PurchaseSourceType.MANUAL,
        source_id=None,
    )
    return replace(candidate, **overrides)


def demand_ref(
    demand_id: uuid.UUID | None = None,
    line_id: uuid.UUID | None = None,
    demand_type: str = "SALES_ORDER",
) -> dict[str, object]:
    return {
        "demand_type": demand_type,
        "demand_id": str(demand_id or uuid.uuid4()),
        "demand_line_id": str(line_id) if line_id else None,
    }


def classify(candidate: CandidateInput, **overrides: object):
    arguments = {
        "vendor_id": VENDOR,
        "item_id": ITEM,
        "warehouse_id": WAREHOUSE,
        "required_date": REQUIRED,
        "source_type": None,
        "source_id": None,
    }
    arguments.update(overrides)
    return classify_candidate(candidate, **arguments)


def test_different_items_in_the_same_procurement_category_are_possible_duplicates():
    result = classify(
        make_candidate(item_id=OTHER_ITEM, item_category_id=CATEGORY),
        item_category_id=CATEGORY,
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.POSSIBLE
    assert "Same procurement category" in result.match_reasons
    assert "Same item" not in result.match_reasons
    assert DuplicateAction.MERGE_WITH_EXISTING in result.available_actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in result.available_actions
    assert result.recommended_action is DuplicateAction.MERGE_WITH_EXISTING


@pytest.mark.parametrize("candidate_category", [None, uuid.uuid4()])
def test_category_signal_requires_the_same_known_procurement_category(candidate_category):
    assert (
        classify(
            make_candidate(item_id=OTHER_ITEM, item_category_id=candidate_category),
            item_category_id=CATEGORY,
        )
        is None
    )


def test_category_signal_does_not_override_the_delivery_window():
    assert (
        classify(
            make_candidate(
                item_id=OTHER_ITEM,
                item_category_id=CATEGORY,
                required_date=REQUIRED + timedelta(days=8),
            ),
            item_category_id=CATEGORY,
        )
        is None
    )


@pytest.mark.parametrize("status", [PurchaseOrderStatus.SENT, PurchaseOrderStatus.ACKNOWLEDGED])
def test_category_merge_requires_an_amendment_but_never_increases_another_item(status):
    result = classify(
        make_candidate(status=status, item_id=OTHER_ITEM, item_category_id=CATEGORY),
        item_category_id=CATEGORY,
    )
    assert result is not None
    assert result.recommended_action is DuplicateAction.MERGE_WITH_EXISTING
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in result.available_actions


def test_a_shared_root_demand_triple_is_exact_even_outside_the_date_window():
    common = demand_ref(line_id=uuid.uuid4())
    result = classify(
        make_candidate(
            source_demand_refs=(demand_ref(), common),
            required_date=REQUIRED + timedelta(days=60),
        ),
        source_demand_refs=(common, demand_ref()),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT
    assert "Both cover the same sales order / BOM demand" in result.match_reasons


def test_demand_identity_matches_uuid_values_and_their_canonical_strings():
    order_id, line_id = uuid.uuid4(), uuid.uuid4()
    persisted = demand_ref(order_id, line_id)
    requested = {
        "demand_type": "SALES_ORDER",
        "demand_id": order_id,
        "demand_line_id": line_id,
    }
    result = classify(
        make_candidate(source_demand_refs=(persisted,)), source_demand_refs=(requested,)
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


@pytest.mark.parametrize("changed_field", ["demand_type", "demand_id", "demand_line_id"])
def test_all_three_demand_identity_fields_must_match(changed_field):
    common = demand_ref(line_id=uuid.uuid4())
    distinct = dict(common)
    distinct[changed_field] = "BOM_DEMAND" if changed_field == "demand_type" else str(uuid.uuid4())
    result = classify(make_candidate(source_demand_refs=(common,)), source_demand_refs=(distinct,))
    assert result is not None
    assert result.confidence is DuplicateConfidence.STRONG


def test_null_demand_line_identity_does_not_match_a_different_named_line():
    common = demand_ref()
    result = classify(
        make_candidate(source_demand_refs=(common,)),
        source_demand_refs=({**common, "demand_line_id": str(uuid.uuid4())},),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.STRONG


def test_one_bom_root_does_not_make_different_component_items_exact_duplicates():
    common = demand_ref(line_id=uuid.uuid4())
    result = classify(
        make_candidate(
            item_id=OTHER_ITEM,
            item_category_id=CATEGORY,
            source_demand_refs=(common,),
        ),
        item_category_id=CATEGORY,
        source_demand_refs=(common,),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.POSSIBLE


@pytest.mark.parametrize("field", ["vendor_id", "warehouse_id"])
def test_demand_identity_cannot_cross_the_vendor_or_warehouse_boundary(field):
    common = demand_ref()
    candidate = make_candidate(source_demand_refs=(common,), **{field: uuid.uuid4()})
    assert classify(candidate, source_demand_refs=(common,)) is None


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"demand_type": "SALES_ORDER", "demand_id": "not-a-uuid"},
        {"demand_type": "", "demand_id": str(uuid.uuid4())},
        {"demand_type": "BOM", "via_item_id": str(uuid.uuid4())},
    ],
)
def test_incomplete_legacy_refs_cannot_create_a_false_exact_match(invalid):
    result = classify(make_candidate(source_demand_refs=(invalid,)), source_demand_refs=(invalid,))
    assert result is not None
    assert result.confidence is DuplicateConfidence.STRONG


def test_every_allocated_requisition_link_is_considered_for_exact_matching():
    first, second = uuid.uuid4(), uuid.uuid4()
    result = classify(
        make_candidate(requisition_line_id=first, requisition_line_ids=frozenset({first, second})),
        requisition_line_id=second,
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


def test_batch_matching_carries_category_and_demand_refs_without_dropping_them():
    common = demand_ref()
    found = classify_candidates(
        [make_candidate(source_demand_refs=(common,))],
        vendor_id=VENDOR,
        item_id=ITEM,
        warehouse_id=WAREHOUSE,
        required_date=REQUIRED,
        source_type=None,
        source_id=None,
        item_category_id=CATEGORY,
        source_demand_refs=(common,),
    )
    assert len(found) == 1
    assert found[0].confidence is DuplicateConfidence.EXACT


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
def test_even_a_draft_or_cancelled_receipt_history_blocks_line_changes(status):
    actions = available_actions_for(
        status=status,
        received_quantity=Decimal("0"),
        has_receipt_history=True,
    )
    assert DuplicateAction.MERGE_WITH_EXISTING not in actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY not in actions


def test_receipt_history_is_exposed_on_the_candidate_even_when_received_is_zero():
    result = classify(make_candidate(has_receipt_history=True))
    assert result is not None
    assert result.has_receipt_history is True
    assert DuplicateAction.MERGE_WITH_EXISTING not in result.available_actions


def test_positive_received_quantity_blocks_changes_even_if_history_flag_was_missing():
    result = classify(make_candidate(received_quantity=Decimal("1")))
    assert result is not None
    assert result.has_receipt_history is True
    assert DuplicateAction.MERGE_WITH_EXISTING not in result.available_actions


def test_demand_types_are_trimmed_and_normalized_by_the_shared_schema():
    common = demand_ref(line_id=uuid.uuid4())
    result = classify(
        make_candidate(source_demand_refs=(common,)),
        source_demand_refs=({**common, "demand_type": " sales_order "},),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


def test_typed_references_and_duplicates_share_one_canonical_identity():
    ref = DemandReference.model_validate(demand_ref(line_id=uuid.uuid4()))
    assert demand_ref_keys((ref, ref.model_dump(), ref.model_dump(mode="json"))) == frozenset(
        {(ref.demand_type, ref.demand_id, ref.demand_line_id)}
    )


def test_a_valid_fifty_reference_pr_list_is_complete_for_duplicate_matching():
    refs = tuple(demand_ref(line_id=uuid.uuid4()) for _ in range(50))
    result = classify(
        make_candidate(source_demand_refs=refs, required_date=REQUIRED + timedelta(days=60)),
        source_demand_refs=(refs[-1],),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


def test_shared_bom_roots_do_not_match_different_items_without_a_category_signal():
    common = demand_ref(line_id=uuid.uuid4())
    assert (
        classify(
            make_candidate(item_id=OTHER_ITEM, source_demand_refs=(common,)),
            source_demand_refs=(common,),
        )
        is None
    )


def test_pooled_incoming_pr_links_match_every_existing_allocation():
    first, second = uuid.uuid4(), uuid.uuid4()
    result = classify(
        make_candidate(requisition_line_ids=frozenset({second})),
        requisition_line_ids=frozenset({first, second}),
    )
    assert result is not None
    assert result.confidence is DuplicateConfidence.EXACT


def test_refusal_exposes_receipt_history_version_and_category_for_action_controls():
    result = classify(
        make_candidate(
            item_category_id=CATEGORY,
            has_receipt_history=True,
            purchase_order_version=7,
        )
    )
    assert result is not None
    assert result.purchase_order_version == 7
    error = duplicate_refusal([result])
    candidate = error.details["candidates"][0]
    assert candidate["item_category_id"] == str(CATEGORY)
    assert candidate["has_receipt_history"] is True
    assert candidate["purchase_order_version"] == 7
    assert DuplicateAction.MERGE_WITH_EXISTING not in candidate["available_actions"]


@pytest.mark.parametrize(
    "status",
    [PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.SENT, PurchaseOrderStatus.ACKNOWLEDGED],
)
def test_unreceived_committed_orders_allow_both_actions_through_amendment(status):
    actions = available_actions_for(status=status, received_quantity=Decimal("0"))
    assert DuplicateAction.MERGE_WITH_EXISTING in actions
    assert DuplicateAction.INCREASE_EXISTING_QUANTITY in actions
