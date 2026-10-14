"""Unsupported noncash adjustments cannot enter payment posting."""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError
from src.modules.finance import service
from src.modules.finance.models import PartyType, PaymentMethod, PaymentType
from src.modules.finance.schemas import PaymentCreate, PaymentUpdate

pytestmark = pytest.mark.unit


def test_create_and_update_reject_adjustment_as_field_validation():
    with pytest.raises(ValidationError, match="Noncash adjustments"):
        PaymentCreate(
            payment_type=PaymentType.PAYMENT,
            party_type=PartyType.VENDOR,
            party_id=uuid.uuid4(),
            payment_date=date(2026, 10, 14),
            amount=Decimal("100"),
            payment_method=PaymentMethod.ADJUSTMENT,
        )
    with pytest.raises(ValidationError, match="Noncash adjustments"):
        PaymentUpdate(payment_method=PaymentMethod.ADJUSTMENT)
    assert PaymentUpdate(notes="Note").payment_method is None


@pytest.mark.parametrize(
    "method", [PaymentMethod.CASH, PaymentMethod.BANK_TRANSFER, PaymentMethod.UPI]
)
def test_supported_money_movement_method_remains_valid(method):
    service.assert_money_movement_method(method)


async def test_posting_seam_refuses_before_any_account_lookup(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("No account or journal query is permitted for a noncash adjustment")

    monkeypatch.setattr(service, "ensure_system_accounts", forbidden)
    monkeypatch.setattr(service, "post_journal", forbidden)
    with pytest.raises(BusinessRuleError, match="Noncash adjustments"):
        await service._post_payment_journal(
            None, None, SimpleNamespace(payment_method=PaymentMethod.ADJUSTMENT)
        )
