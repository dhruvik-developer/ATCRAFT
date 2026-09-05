"""A late arrival must carry a delay reason, whichever path records it.

``confirm_delivery`` always asked. ``add_tracking_event`` did not, so the
timeline dialog - and any courier feed - could walk a consignment to DELIVERED
after its promised date with nothing recorded against the delay, and the
on-time-delivery report grew rows nobody could explain. Both paths now call
:func:`assert_delay_explained`; this pins the rule itself.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.errors import BusinessRuleError
from src.modules.dispatch.service import assert_delay_explained

pytestmark = pytest.mark.unit

PROMISED = date(2026, 9, 3)


def test_arriving_on_the_promised_day_needs_no_reason() -> None:
    late = assert_delay_explained(
        code="DSP/2026-27/00001",
        expected_delivery_date=PROMISED,
        actual_delivery_date=PROMISED,
        reason=None,
    )
    assert late is False


def test_arriving_early_needs_no_reason() -> None:
    assert (
        assert_delay_explained(
            code="DSP/2026-27/00001",
            expected_delivery_date=PROMISED,
            actual_delivery_date=date(2026, 9, 1),
            reason=None,
        )
        is False
    )


def test_a_consignment_never_promised_cannot_be_late() -> None:
    assert (
        assert_delay_explained(
            code="DSP/2026-27/00001",
            expected_delivery_date=None,
            actual_delivery_date=date(2026, 9, 30),
            reason=None,
        )
        is False
    )


def test_a_late_arrival_with_a_reason_is_reported_late() -> None:
    late = assert_delay_explained(
        code="DSP/2026-27/00001",
        expected_delivery_date=PROMISED,
        actual_delivery_date=date(2026, 9, 5),
        reason="Transporter strike at the Pune hub",
    )
    assert late is True


def test_a_late_arrival_without_a_reason_is_refused() -> None:
    with pytest.raises(BusinessRuleError) as exc:
        assert_delay_explained(
            code="DSP/2026-27/00001",
            expected_delivery_date=PROMISED,
            actual_delivery_date=date(2026, 9, 5),
            reason=None,
        )
    assert "delay reason is required" in exc.value.message
    assert exc.value.details == {
        "expected_delivery_date": "2026-09-03",
        "actual_delivery_date": "2026-09-05",
        "delay_days": 2,
    }


def test_a_blank_reason_does_not_count() -> None:
    with pytest.raises(BusinessRuleError):
        assert_delay_explained(
            code="DSP/2026-27/00001",
            expected_delivery_date=PROMISED,
            actual_delivery_date=date(2026, 9, 5),
            reason="",
        )
