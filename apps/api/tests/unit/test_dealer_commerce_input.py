"""Portal inputs cannot select their tenant, financial owner or QC decision."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.modules.dealer_portal.commerce_schemas import DealerReturnCreate
from src.modules.dealer_portal.promotion_schemas import (
    DealerBannerCreate,
    DealerOfferCreate,
    PromotionRetire,
)


def return_payload():
    return {
        "dispatch_id": uuid.uuid4(),
        "lines": [{"dispatch_line_id": uuid.uuid4(), "quantity": "1", "reason_code": "DAMAGED"}],
    }


@pytest.mark.parametrize(
    "field",
    [
        "company_id",
        "customer_id",
        "return_warehouse_id",
        "source",
        "status",
        "requested_by_user_id",
    ],
)
def test_return_request_rejects_staff_controlled_fields(field):
    with pytest.raises(ValidationError):
        DealerReturnCreate.model_validate({**return_payload(), field: str(uuid.uuid4())})


@pytest.mark.parametrize(
    "field", ["requires_qc", "qc_inspection_id", "serial_ids", "credited_quantity"]
)
def test_return_line_cannot_override_receipt_or_inspection(field):
    payload = return_payload()
    payload["lines"][0][field] = None
    with pytest.raises(ValidationError):
        DealerReturnCreate.model_validate(payload)


@pytest.mark.parametrize(
    "quantity", ["0", "-1", "NaN", "Infinity", "1.00001", "1000000000000000000"]
)
def test_return_request_requires_bounded_finite_positive_quantity(quantity):
    payload = return_payload()
    payload["lines"][0]["quantity"] = quantity
    with pytest.raises(ValidationError):
        DealerReturnCreate.model_validate(payload)


def test_return_request_rejects_duplicate_lines():
    payload = return_payload()
    payload["lines"] *= 2
    with pytest.raises(ValidationError, match="only once"):
        DealerReturnCreate.model_validate(payload)


@pytest.mark.parametrize(
    "scope,target",
    [("ALL", "item_id"), ("ITEM", None), ("BRAND", "category_id"), ("CATEGORY", "brand_id")],
)
def test_offer_scope_names_exactly_its_target(scope, target):
    data = {
        "code": "OFFER",
        "title": "Winter offer",
        "scope_kind": scope,
        "discount_percentage": "10",
    }
    if target:
        data[target] = uuid.uuid4()
    with pytest.raises(ValidationError, match="targets"):
        DealerOfferCreate.model_validate(data)


@pytest.mark.parametrize("discount", ["0", "-1", "100.01", "NaN", "Infinity"])
def test_offer_discount_is_finite_and_bounded(discount):
    with pytest.raises(ValidationError):
        DealerOfferCreate(
            code="OFFER", title="Winter offer", scope_kind="ALL", discount_percentage=discount
        )


def test_offer_dates_and_display_labels_are_validated():
    data = {
        "code": " OFFER ",
        "title": " Winter offer ",
        "scope_kind": "ALL",
        "discount_percentage": "10",
    }
    offer = DealerOfferCreate(**data)
    assert offer.code == "OFFER" and offer.title == "Winter offer"
    with pytest.raises(ValidationError):
        DealerOfferCreate(**{**data, "title": "  "})
    with pytest.raises(ValidationError, match="end date"):
        DealerOfferCreate(**data, valid_from=date(2026, 10, 24), valid_to=date(2026, 10, 23))


@pytest.mark.parametrize("kind,target", [("NONE", uuid.uuid4()), ("ITEM", None)])
def test_banner_link_is_complete(kind, target):
    with pytest.raises(ValidationError, match="link"):
        DealerBannerCreate(
            code="BANNER",
            title="New products",
            image_document_id=uuid.uuid4(),
            link_kind=kind,
            link_id=target,
        )


@pytest.mark.parametrize("reason", ["", "  ", " a ", "x" * 501])
def test_promotion_lifecycle_requires_a_meaningful_reason(reason):
    with pytest.raises(ValidationError):
        PromotionRetire(reason=reason)


def test_promotion_reason_is_normalized_and_future_window_is_allowed():
    assert PromotionRetire(reason="  Seasonal refresh  ").reason == "Seasonal refresh"
    offer = DealerOfferCreate(
        code="FUTURE",
        title="Coming soon",
        scope_kind="ALL",
        discount_percentage="10",
        valid_from=date(2026, 10, 24),
        valid_to=date(2026, 10, 24) + timedelta(days=7),
    )
    assert not offer.is_active
