"""Two quotation rules that decide what gets stored, without a database.

* **A zero-day term is a value, not an omission.** ``create_quotation`` used
  ``payload.lead_time_days or vendor.lead_time_days``, so an ex-stock quote
  (0 days) was stored with the vendor master's lead time - which changes the
  comparison score and can hand the recommendation to a slower vendor. The
  schema now distinguishes "omitted" (``None``) from ``0`` and the service
  defaults only the former.

* **A rejected or lapsed offer can be revised.** The row is reopened through
  the state machine rather than a second row being inserted for the same
  (RFQ, vendor). The lifecycle half of that lives in
  ``test_procurement_lifecycle``; here the service's own gate is pinned.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from src.modules.procurement.models import QuotationStatus
from src.modules.procurement.schemas import QuotationCreate, QuotationLineIn, QuotationUpdate
from src.modules.procurement.service import quotation_terms
from src.modules.procurement.service_rules import (
    QUOTATION_LIFECYCLE,
    REQUOTABLE_QUOTATION_STATUSES,
)

pytestmark = pytest.mark.unit


class _Vendor:
    """Only the two columns the rule reads."""

    def __init__(self, payment_terms_days: int = 30, lead_time_days: int = 21) -> None:
        self.payment_terms_days = payment_terms_days
        self.lead_time_days = lead_time_days


def _line() -> QuotationLineIn:
    return QuotationLineIn(
        line_number=1,
        item_id=uuid.uuid4(),
        quantity=Decimal("10"),
        unit_price=Decimal("5"),
    )


def _create(**overrides: Any) -> QuotationCreate:
    return QuotationCreate(
        rfq_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        vendor_reference="Q-1",
        lines=[_line()],
        **overrides,
    )


# -----------------------------------------------------------------------------
# Zero is a value
# -----------------------------------------------------------------------------


def test_omitted_terms_mean_the_vendors_agreed_terms() -> None:
    payload = _create()
    assert payload.payment_terms_days is None
    assert payload.lead_time_days is None

    assert quotation_terms(
        payment_terms_days=payload.payment_terms_days,
        lead_time_days=payload.lead_time_days,
        vendor=_Vendor(30, 21),  # type: ignore[arg-type]
    ) == (30, 21)


def test_an_explicit_zero_is_stored_as_zero() -> None:
    """An ex-stock quote and a cash quote are both real, and both score differently."""
    payload = _create(payment_terms_days=0, lead_time_days=0)
    assert payload.payment_terms_days == 0
    assert payload.lead_time_days == 0

    assert quotation_terms(
        payment_terms_days=payload.payment_terms_days,
        lead_time_days=payload.lead_time_days,
        vendor=_Vendor(30, 21),  # type: ignore[arg-type]
    ) == (0, 0)


def test_each_term_defaults_independently() -> None:
    assert quotation_terms(
        payment_terms_days=0,
        lead_time_days=None,
        vendor=_Vendor(45, 14),  # type: ignore[arg-type]
    ) == (0, 14)
    assert quotation_terms(
        payment_terms_days=None,
        lead_time_days=0,
        vendor=_Vendor(45, 14),  # type: ignore[arg-type]
    ) == (45, 0)


def test_create_and_update_agree_on_what_omitted_means() -> None:
    """The two payloads used to disagree: update accepted 0, create swallowed it."""
    assert QuotationUpdate(lead_time_days=0).lead_time_days == 0
    assert QuotationUpdate().lead_time_days is None
    assert _create(lead_time_days=0).lead_time_days == 0
    assert _create().lead_time_days is None


# -----------------------------------------------------------------------------
# Revising a rejected offer
# -----------------------------------------------------------------------------


def test_a_revision_may_carry_the_reason_it_reopens_the_offer_with() -> None:
    assert QuotationUpdate(reason="Revised price after negotiation").reason is not None
    assert QuotationUpdate().reason is None


def test_the_requotable_set_is_exactly_what_the_machine_reopens() -> None:
    """``update_quotation`` reads the set; the machine must offer the edge for each."""
    reopenable = {
        status
        for status in QuotationStatus
        if QUOTATION_LIFECYCLE.can_transition(status, QuotationStatus.RECEIVED)
    }
    assert reopenable == set(REQUOTABLE_QUOTATION_STATUSES)
    # And nothing terminal is in it: a selected offer is the one a PO is raised against.
    assert not any(QUOTATION_LIFECYCLE.is_terminal(s) for s in REQUOTABLE_QUOTATION_STATUSES)
