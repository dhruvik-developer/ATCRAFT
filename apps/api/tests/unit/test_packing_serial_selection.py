"""Named FINAL selection, inspection ownership and carton membership invariants."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.packing.models import PackingOrderLine
from src.modules.packing.schemas import (
    PackingReinspectRequest,
    PackingSerialSelection,
    PackingStartRequest,
)
from src.modules.packing.serial_tracking import (
    SerialContent,
    assert_named_verdicts,
    assert_selected_contents,
    select_final_units,
)


@pytest.fixture
def kit():
    item_id, line_id, inspection_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ids = [uuid.uuid4(), uuid.uuid4()]
    line = PackingOrderLine(
        id=line_id,
        item_id=item_id,
        line_number=1,
        quantity_to_pack=Decimal("2"),
        qc_inspection_id=inspection_id,
        qc_serial_ids=ids,
        qc_batch_id=None,
    )
    item = SimpleNamespace(is_serial_tracked=True, requires_final_qc=True)
    inspection = SimpleNamespace(
        id=inspection_id,
        item_id=item_id,
        serial_ids=ids,
        serial_dispositions={str(unit): "ACCEPTED" for unit in ids},
        batch_id=None,
        reference_type="packing_order",
        reference_id=line_id,
        qc_stage="FINAL",
        inspected_quantity=2,
    )
    return SimpleNamespace(line=line, items={item_id: item}, ids=ids, inspection=inspection)


def test_reason_only_lifecycle_requests_remain_valid():
    assert PackingStartRequest(reason="Existing quantity packing").serial_selections == []
    assert PackingReinspectRequest(reason="Existing quantity replacement").serial_ids == []


def test_request_rejects_duplicate_lines_and_duplicate_units(kit):
    k = kit
    with pytest.raises(ValidationError, match="only once"):
        PackingSerialSelection(line_id=k.line.id, serial_ids=k.ids * 2)
    selection = PackingSerialSelection(line_id=k.line.id, serial_ids=k.ids)
    with pytest.raises(ValidationError, match="one serial selection"):
        PackingStartRequest(reason="Inspect units", serial_selections=[selection, selection])
    with pytest.raises(ValidationError, match="only once"):
        PackingReinspectRequest(reason="Inspect replacements", serial_ids=k.ids * 2)


def test_selection_cannot_name_another_orders_line(kit):
    with pytest.raises(NotFoundError):
        select_final_units(
            [kit.line],
            kit.items,
            [PackingSerialSelection(line_id=uuid.uuid4(), serial_ids=kit.ids)],
        )


def test_quantity_only_lines_cannot_accept_unrelated_serial_selection(kit):
    next(iter(kit.items.values())).is_serial_tracked = False
    with pytest.raises(BusinessRuleError, match="only to lines"):
        select_final_units(
            [kit.line], kit.items, [PackingSerialSelection(line_id=kit.line.id, serial_ids=kit.ids)]
        )


def test_a_physical_unit_cannot_be_selected_for_two_lines(kit):
    other = PackingOrderLine(
        id=uuid.uuid4(),
        item_id=kit.line.item_id,
        line_number=2,
        quantity_to_pack=Decimal("2"),
        qc_serial_ids=kit.ids,
    )
    with pytest.raises(BusinessRuleError, match="two packing lines"):
        select_final_units([kit.line, other], kit.items, [])


@pytest.mark.parametrize(
    "change",
    [
        {"serial_ids": []},
        {"serial_ids": [uuid.uuid4(), uuid.uuid4()]},
        {"batch_id": uuid.uuid4()},
        {"item_id": uuid.uuid4()},
        {"reference_id": uuid.uuid4()},
        {"reference_type": "manual"},
        {"qc_stage": "INCOMING"},
        {"inspected_quantity": 1},
        {"serial_dispositions": {}},
    ],
)
def test_quantity_or_unrelated_acceptance_cannot_approve_named_units(kit, change):
    for field, value in change.items():
        setattr(kit.inspection, field, value)
    with pytest.raises(BusinessRuleError, match="exact selected serial"):
        assert_named_verdicts([kit.line], kit.items, {kit.inspection.id: kit.inspection})


def test_dominant_accepted_result_cannot_approve_a_rejected_unit(kit):
    kit.inspection.serial_dispositions[str(kit.ids[1])] = "REJECTED"
    with pytest.raises(BusinessRuleError, match="ACCEPTED FINAL inspection"):
        assert_named_verdicts([kit.line], kit.items, {kit.inspection.id: kit.inspection})


def test_named_acceptance_and_split_cartons_are_valid(kit):
    assert_named_verdicts([kit.line], kit.items, {kit.inspection.id: kit.inspection})
    assert_selected_contents(
        [SerialContent(kit.line.id, None, [unit]) for unit in kit.ids], [kit.line], kit.items
    )


@pytest.mark.parametrize(
    "contents_kind", ["duplicate_rows", "duplicate_units", "wrong_batch", "substitution"]
)
def test_carton_membership_rejects_duplicate_substituted_or_wrong_batch_units(kit, contents_kind):
    rows = [SerialContent(kit.line.id, None, kit.ids)]
    if contents_kind == "duplicate_rows":
        rows *= 2
    elif contents_kind == "duplicate_units":
        rows = [SerialContent(kit.line.id, None, kit.ids[:1] * 2)]
    elif contents_kind == "wrong_batch":
        rows = [SerialContent(kit.line.id, uuid.uuid4(), kit.ids)]
    else:
        rows = [SerialContent(kit.line.id, None, [kit.ids[0], uuid.uuid4()])]
    with pytest.raises(BusinessRuleError, match="selected FINAL-QC serials"):
        assert_selected_contents(rows, [kit.line], kit.items)
