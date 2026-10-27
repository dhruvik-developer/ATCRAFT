"""Commercial terms cannot be silently averaged or dropped during a merge."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.core.errors import ValidationError
from src.modules.procurement.models import PurchaseOrder, PurchaseOrderLine
from src.modules.procurement.purchase_order_changes import _assert_terms, _same_line_terms
from src.modules.procurement.schemas import PurchaseOrderCreate, PurchaseOrderLineIn


@pytest.fixture
def terms():
    vendor_id, warehouse_id, item_id, uom_id = (uuid.uuid4() for _ in range(4))
    required = date(2030, 1, 10)
    order = PurchaseOrder(
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        expected_delivery_date=required,
        currency="INR",
        payment_terms_days=30,
        terms=None,
        delivery_address=None,
        priority=5,
    )
    existing = PurchaseOrderLine(
        line_number=1,
        item_id=item_id,
        description="Component as agreed",
        quantity=Decimal("100"),
        uom_id=uom_id,
        unit_price=Decimal("50"),
        discount_percentage=Decimal("0"),
        discount=Decimal("0"),
        tax_rate=Decimal("18"),
        required_date=None,
        requires_qc=False,
        quotation_line_id=None,
        notes=None,
    )
    incoming = PurchaseOrderLineIn(
        line_number=1, item_id=item_id, quantity=Decimal("25"), unit_price=Decimal("50")
    )
    draft = PurchaseOrderCreate(
        vendor_id=vendor_id,
        warehouse_id=warehouse_id,
        expected_delivery_date=required,
        lines=[incoming],
    )
    return order, existing, incoming, draft, uom_id


def test_default_unit_and_zero_discount_are_economically_identical(terms):
    order, existing, incoming, _, uom_id = terms
    assert _same_line_terms(existing, incoming, Decimal("18"), order, uom_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("item_id", uuid.uuid4()),
        ("uom_id", uuid.uuid4()),
        ("unit_price", Decimal("50.000001")),
        ("discount_percentage", Decimal("0.0001")),
        ("discount", Decimal("0.01")),
        ("required_date", date(2030, 1, 11)),
        ("requires_qc", True),
        ("quotation_line_id", uuid.uuid4()),
        ("notes", "Apply the other process"),
        ("description", "Another agreed specification"),
    ],
)
def test_a_commercial_line_difference_cannot_be_absorbed(terms, field, value):
    order, existing, incoming, _, uom_id = terms
    proposed = incoming.model_copy(update={field: value})
    assert not _same_line_terms(existing, proposed, Decimal("18"), order, uom_id)


def test_item_tax_change_cannot_be_hidden_by_merging_at_the_old_price(terms):
    order, existing, incoming, _, uom_id = terms
    assert not _same_line_terms(existing, incoming, Decimal("12"), order, uom_id)


def test_percentage_and_flat_discounts_are_not_interchangeable(terms):
    order, existing, incoming, _, uom_id = terms
    existing.discount_percentage = Decimal("10")
    existing.discount = Decimal("500")
    flat = incoming.model_copy(update={"discount": Decimal("125")})
    assert not _same_line_terms(existing, flat, Decimal("18"), order, uom_id)
    percentage = incoming.model_copy(update={"discount_percentage": Decimal("10")})
    assert _same_line_terms(existing, percentage, Decimal("18"), order, uom_id)


def test_header_defaults_resolve_to_the_vendors_existing_payment_terms(terms):
    order, _, _, draft, _ = terms
    _assert_terms(order, draft, 30)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor_id", uuid.uuid4()),
        ("warehouse_id", uuid.uuid4()),
        ("currency", "USD"),
        ("payment_terms_days", 31),
        ("terms", "Different payment agreement"),
        ("delivery_address", "Different destination address"),
        ("expected_delivery_date", date(2030, 1, 11)),
        ("branch_id", uuid.uuid4()),
        ("priority", 3),
        ("assigned_vendor_user_id", uuid.uuid4()),
        ("notes", "A private incoming instruction"),
        ("vendor_reference", "ANOTHER-OFFER"),
        ("freight_amount", Decimal("1")),
    ],
)
def test_meaningful_incoming_header_terms_cannot_be_discarded(terms, field, value):
    order, _, _, draft, _ = terms
    proposed = draft.model_copy(update={field: value})
    with pytest.raises(ValidationError, match="Use a separate purchase order"):
        _assert_terms(order, proposed, 30)
