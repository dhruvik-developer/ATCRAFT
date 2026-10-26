from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError, InvalidStateTransitionError
from src.modules.inventory.models import SerialStatus, StockState, TransactionType
from src.modules.inventory.service import serial_transition
from src.modules.job_work.schemas import IssueLineIn, ReceiptLineIn, ReceiveRequest
from src.modules.quality.models import QcInspection
from src.modules.quality.schemas import DecideRequest, InspectionCreate
from src.modules.quality.serial_tracking import validate_serial_decision

pytestmark = pytest.mark.unit


def test_vendor_processing_consumes_the_issued_input_without_issuing_it_twice():
    assert (
        serial_transition(
            serial_number="UNIT1",
            current=SerialStatus.ISSUED,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            landing_state=None,
            source_state=StockState.JOB_WORK,
        )
        is None
    )
    with pytest.raises(InvalidStateTransitionError):
        serial_transition(
            serial_number="UNIT1",
            current=SerialStatus.ISSUED,
            transaction_type=TransactionType.MATERIAL_ISSUE,
            landing_state=None,
            source_state=StockState.AVAILABLE,
        )


@pytest.mark.parametrize(
    "source", [None, StockState.AVAILABLE, StockState.QC_HOLD, StockState.JOB_WORK]
)
def test_adjustments_cannot_write_off_an_issued_serial(source):
    with pytest.raises(BusinessRuleError):
        serial_transition(
            serial_number="UNIT1",
            current=SerialStatus.ISSUED,
            transaction_type=TransactionType.ADJUSTMENT_DECREASE,
            landing_state=None,
            source_state=source,
        )


def test_only_vendor_pool_scrap_retires_an_issued_serial():
    assert (
        serial_transition(
            serial_number="UNIT1",
            current=SerialStatus.ISSUED,
            transaction_type=TransactionType.SCRAP,
            landing_state=StockState.SCRAP,
            source_state=StockState.JOB_WORK,
        )
        is SerialStatus.SCRAPPED
    )
    with pytest.raises(BusinessRuleError):
        serial_transition(
            serial_number="UNIT1",
            current=SerialStatus.ISSUED,
            transaction_type=TransactionType.SCRAP,
            landing_state=StockState.SCRAP,
            source_state=StockState.AVAILABLE,
        )


def test_issue_and_receipt_reject_duplicate_or_ambiguous_unit_selections():
    serial_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="only once"):
        IssueLineIn(line_number=1, issued_quantity=2, serial_ids=[serial_id, serial_id])
    with pytest.raises(ValidationError, match="every issued"):
        IssueLineIn(line_number=1, issued_quantity=2, serial_ids=[serial_id])
    with pytest.raises(ValidationError, match="only one disposition"):
        ReceiptLineIn(
            line_number=1,
            output_quantity=1,
            output_serial_ids=[serial_id],
            scrapped_serial_ids=[serial_id],
            is_final=True,
        )


def _inspection():
    ids = [uuid.uuid4(), uuid.uuid4()]
    return QcInspection(serial_ids=ids, serial_dispositions={}), ids


@pytest.mark.parametrize("count", [500, 501])
def test_one_receipt_bounds_all_named_dispositions_across_multiple_lines(count):
    ids = [uuid.uuid4() for _ in range(count)]
    values = {
        "reason": "Return physical units in bounded partial receipts",
        "lines": [
            {"line_number": 1, "output_quantity": 200, "output_serial_ids": ids[:200]},
            {
                "line_number": 2,
                "output_quantity": 100,
                "output_serial_ids": ids[200:300],
                "rejected_quantity": 100,
                "rejected_serial_ids": ids[300:400],
                "returned_unprocessed_quantity": 50,
                "returned_unprocessed_serial_ids": ids[400:450],
                "scrapped_serial_ids": ids[450:],
                "is_final": True,
            },
        ],
    }
    if count == 500:
        assert len(ReceiveRequest.model_validate(values).lines) == 2
    else:
        with pytest.raises(ValidationError, match="at most 500 serial units across all"):
            ReceiveRequest.model_validate(values)


def test_qc_counts_must_match_named_unit_dispositions():
    inspection, ids = _inspection()
    payload = DecideRequest(
        accepted_quantity=Decimal("1"),
        rejected_quantity=Decimal("1"),
        reason="Inspect returned lot",
        rejection_reason="Damaged finish",
        serial_dispositions={ids[0]: "ACCEPTED", ids[1]: "REJECTED"},
    )
    assert validate_serial_decision(inspection, payload) == {
        str(ids[0]): "ACCEPTED",
        str(ids[1]): "REJECTED",
    }
    with pytest.raises(BusinessRuleError, match="match the named"):
        validate_serial_decision(
            inspection, payload.model_copy(update={"accepted_quantity": Decimal("2")})
        )


def test_qc_requires_exact_units_and_cannot_swap_accepted_and_rejected_identities():
    inspection, ids = _inspection()
    with pytest.raises(BusinessRuleError, match="every serial"):
        validate_serial_decision(
            inspection, DecideRequest(accepted_quantity=2, reason="Accepted lot")
        )
    inspection.serial_dispositions = {str(ids[0]): "ACCEPTED", str(ids[1]): "REWORK"}
    with pytest.raises(BusinessRuleError, match="cannot be dispositioned again"):
        validate_serial_decision(
            inspection,
            DecideRequest(
                accepted_quantity=1,
                rejected_quantity=1,
                reason="Swap units",
                rejection_reason="Wrong identity",
                serial_dispositions={ids[0]: "REJECTED", ids[1]: "ACCEPTED"},
            ),
        )


def test_qc_rework_retains_the_original_accepted_unit_while_settling_the_other():
    inspection, ids = _inspection()
    inspection.serial_dispositions = {str(ids[0]): "ACCEPTED", str(ids[1]): "REWORK"}
    proposed = validate_serial_decision(
        inspection,
        DecideRequest(
            accepted_quantity=2,
            reason="Reworked unit accepted",
            serial_dispositions={ids[0]: "ACCEPTED", ids[1]: "ACCEPTED"},
        ),
    )
    assert set(proposed.values()) == {"ACCEPTED"}


def test_non_serial_inspection_cannot_silently_ignore_serial_dispositions():
    with pytest.raises(BusinessRuleError, match="does not cover"):
        validate_serial_decision(
            QcInspection(serial_ids=[]),
            DecideRequest(
                accepted_quantity=1,
                reason="Accepted unit",
                serial_dispositions={uuid.uuid4(): "ACCEPTED"},
            ),
        )


def test_manual_inspection_requires_one_distinct_unit_per_quantity():
    serial_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="only once"):
        InspectionCreate(
            item_id=uuid.uuid4(),
            warehouse_id=uuid.uuid4(),
            qc_stage="INCOMING",
            inspected_quantity=2,
            serial_ids=[serial_id, serial_id],
            reason="Inspect units",
        )
