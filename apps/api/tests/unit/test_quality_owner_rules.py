"""The owners' own small rules for applying a quality verdict (WP E4).

* procurement: what a receipt line's QC status becomes once its lot is decided
  (docs/assumptions.md §E4-2);
* job work: a returned lot is inspected one output item at a time.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.job_work.service import QC_REFERENCE_KEYS, held_output_by_item
from src.modules.procurement.models import ReceiptLineQcStatus
from src.modules.procurement.service_rules import receipt_line_qc_status

pytestmark = pytest.mark.unit


def test_a_lot_accepted_in_full_passes() -> None:
    assert receipt_line_qc_status(settled=True, refused_quantity=Decimal("0")) is (
        ReceiptLineQcStatus.PASSED
    )


@pytest.mark.parametrize("refused", ["0.0001", "5", "80"])
def test_any_refused_share_fails_the_line(refused: str) -> None:
    assert receipt_line_qc_status(settled=True, refused_quantity=Decimal(refused)) is (
        ReceiptLineQcStatus.FAILED
    )


def test_a_lot_still_partly_on_hold_leaves_the_line_pending() -> None:
    assert receipt_line_qc_status(settled=False, refused_quantity=Decimal("0")) is None
    assert receipt_line_qc_status(settled=False, refused_quantity=Decimal("3")) is None


def test_a_lot_is_inspected_one_output_item_at_a_time() -> None:
    chrome, brass = uuid.uuid4(), uuid.uuid4()
    held = held_output_by_item(
        [
            (chrome, Decimal("60")),
            (brass, Decimal("40")),
            (chrome, Decimal("15")),
            (brass, Decimal("0")),
        ]
    )
    assert held == {chrome: Decimal("75.0000"), brass: Decimal("40.0000")}
    # In the order the lot first names them, so the first inspection is the
    # one the receipt header keeps.
    assert list(held) == [chrome, brass]


def test_a_lot_with_no_good_output_raises_no_inspection() -> None:
    assert held_output_by_item([(uuid.uuid4(), Decimal("0"))]) == {}


def test_a_lot_is_read_under_its_own_key_and_the_one_it_had_before() -> None:
    assert QC_REFERENCE_KEYS == ("job_work_receipt", "job_work_order")
