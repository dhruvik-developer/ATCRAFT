from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError
from src.modules.collections.models import FOLLOW_UP_REMINDER_DAYS
from src.modules.collections.service import reminder_due, validate_promise


def test_promise_accepts_today_and_amount_within_the_overdue_balance() -> None:
    assert validate_promise(
        promised_amount=Decimal("125.50"),
        promised_date=date(2026, 11, 22),
        overdue_amount=Decimal("200"),
        as_of=date(2026, 11, 22),
    ) == Decimal("125.5000")


@pytest.mark.parametrize(
    ("promised_amount", "promised_date", "message"),
    [
        (Decimal("10"), date(2026, 11, 21), "promise date"),
        (Decimal("201"), date(2026, 11, 22), "cannot exceed"),
    ],
)
def test_promise_rejects_past_dates_and_amounts_above_the_balance(
    promised_amount: Decimal, promised_date: date, message: str
) -> None:
    with pytest.raises(BusinessRuleError, match=message):
        validate_promise(
            promised_amount=promised_amount,
            promised_date=promised_date,
            overdue_amount=Decimal("200"),
            as_of=date(2026, 11, 22),
        )


def test_reminder_cadence_is_exact_and_repeats_only_after_three_days() -> None:
    now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    last_reminded = now - timedelta(days=FOLLOW_UP_REMINDER_DAYS)
    assert reminder_due(None, now)
    assert reminder_due(last_reminded, now)
    assert not reminder_due(last_reminded + timedelta(seconds=1), now)
