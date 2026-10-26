"""Invalid credit bases fail before any financial document or journal is written."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.errors import NotFoundError
from src.modules.finance.credit_note_schemas import CreditNoteCreate, CreditNoteLineIn
from src.modules.finance.models import CreditNoteReason
from src.modules.quality.models import QcInspection
from src.modules.sales_returns.crediting import accepted_return_quantity
from src.modules.sales_returns.models import SalesReturnLine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "basis",
    [
        {},
        {"quantity": "1", "taxable_amount": "10"},
        {"quantity": "0"},
        {"quantity": "-1"},
        {"quantity": "NaN"},
        {"quantity": "Infinity"},
        {"quantity": "0.00001"},
        {"quantity": "100000000000000"},
        {"taxable_amount": "0"},
        {"taxable_amount": "0.00001"},
    ],
)
def test_invalid_credit_basis_is_rejected(basis):
    with pytest.raises(ValidationError):
        CreditNoteLineIn(sales_invoice_line_id=uuid.uuid4(), **basis)


@pytest.mark.parametrize("basis", [{"quantity": "1.0001"}, {"taxable_amount": "10.0001"}])
def test_one_valid_credit_basis_is_preserved(basis):
    line = CreditNoteLineIn(sales_invoice_line_id=uuid.uuid4(), **basis)
    for key, value in basis.items():
        assert getattr(line, key) == Decimal(value)


def _payload(**overrides):
    return {
        "sales_invoice_id": uuid.uuid4(),
        "note_date": date(2026, 10, 24),
        "reason_type": CreditNoteReason.PRICE_ADJUSTMENT,
        "lines": [{"sales_invoice_line_id": uuid.uuid4(), "quantity": "1"}],
        **overrides,
    }


def test_duplicate_invoice_lines_are_validation_errors():
    line = {"sales_invoice_line_id": uuid.uuid4(), "quantity": "1"}
    with pytest.raises(ValidationError, match="only once"):
        CreditNoteCreate.model_validate(_payload(lines=[line, line]))


def test_return_reason_requires_its_document():
    with pytest.raises(ValidationError, match="reference a sales return"):
        CreditNoteCreate.model_validate(_payload(reason_type=CreditNoteReason.RETURN))


def test_return_link_cannot_bypass_a_non_return_credit_policy():
    with pytest.raises(ValidationError, match="Only a return credit"):
        CreditNoteCreate.model_validate(_payload(sales_return_id=uuid.uuid4()))


def test_return_credit_requires_a_physical_quantity():
    with pytest.raises(ValidationError, match="accepted quantities"):
        CreditNoteCreate.model_validate(
            _payload(
                reason_type=CreditNoteReason.RETURN,
                sales_return_id=uuid.uuid4(),
                lines=[{"sales_invoice_line_id": uuid.uuid4(), "taxable_amount": "10"}],
            )
        )


@pytest.mark.parametrize("requires_qc, expected", [(False, "3"), (True, "0")])
def test_return_without_inspection_only_credits_non_qc_stock(requires_qc, expected):
    line = SalesReturnLine(
        quantity=Decimal("3"), requires_qc=requires_qc, serial_ids=[], qc_inspection_id=None
    )
    assert accepted_return_quantity(line, None) == Decimal(expected)


def test_serial_credit_counts_only_accepted_units():
    serial_ids = [uuid.uuid4() for _ in range(3)]
    line = SalesReturnLine(
        quantity=Decimal("3"), serial_ids=serial_ids, accepted_serial_ids=serial_ids[:1]
    )
    assert accepted_return_quantity(line, None) == Decimal("1")


def test_qc_credit_uses_accepted_quantity_and_missing_inspection_fails_closed():
    inspection_id = uuid.uuid4()
    line = SalesReturnLine(
        quantity=Decimal("3"), requires_qc=True, serial_ids=[], qc_inspection_id=inspection_id
    )
    assert accepted_return_quantity(line, QcInspection(accepted_quantity=Decimal("1"))) == 1
    with pytest.raises(NotFoundError):
        accepted_return_quantity(line, None)
