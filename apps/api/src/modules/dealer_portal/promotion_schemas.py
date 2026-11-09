"""Validated promotion content; scope targets and date windows stay consistent."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.constants import REASON_MIN_LENGTH
from src.core.messages import REASON_TOO_SHORT
from src.modules.dealer_portal.models import (
    DealerBannerLinkKind,
    DealerBannerStatus,
    OfferScopeKind,
)
from src.modules.products.models import PriceType

#: Each constrained field is defined once and used by the create *and* the edit,
#: so an edit is refused when the request is parsed - naming its field - by
#: exactly the rules a create is, and the contract states those rules for both.
PromotionTitle = Annotated[str, Field(min_length=1, max_length=160)]
PromotionText = Annotated[str, Field(max_length=2000)]
SortOrder = Annotated[int, Field(ge=0, le=100000)]
MinimumQuantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
DiscountPercentage = Annotated[Decimal, Field(gt=0, le=100, max_digits=9, decimal_places=4)]


class PromotionFields(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_/-]*$")
    title: PromotionTitle
    audience_price_type: PriceType | None = None
    sort_order: SortOrder = 0
    valid_from: date | None = None
    valid_to: date | None = None

    @field_validator("code", "title", mode="before")
    @classmethod
    def trim_label(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def ordered_dates(self) -> PromotionFields:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("The validity end date must be on or after the start date.")
        return self


class DealerBannerCreate(PromotionFields):
    subtitle: PromotionText | None = None
    image_document_id: uuid.UUID
    link_kind: DealerBannerLinkKind = DealerBannerLinkKind.NONE
    link_id: uuid.UUID | None = None
    status: DealerBannerStatus = DealerBannerStatus.INACTIVE

    @model_validator(mode="after")
    def matching_link(self) -> DealerBannerCreate:
        if (self.link_kind is DealerBannerLinkKind.NONE) != (self.link_id is None):
            raise ValueError("A banner link needs exactly the target required by its link kind.")
        if self.status is DealerBannerStatus.ARCHIVED:
            raise ValueError("Use the retire action to archive a banner with a reason.")
        return self


class DealerBannerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    title: str
    subtitle: str | None
    image_document_id: uuid.UUID
    link_kind: DealerBannerLinkKind
    link_id: uuid.UUID | None
    audience_price_type: PriceType | None
    sort_order: int
    valid_from: date | None
    valid_to: date | None
    status: DealerBannerStatus


class DealerBannerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: PromotionTitle | None = None
    subtitle: PromotionText | None = None
    image_document_id: uuid.UUID | None = None
    link_kind: DealerBannerLinkKind | None = None
    link_id: uuid.UUID | None = None
    audience_price_type: PriceType | None = None
    sort_order: SortOrder | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status: DealerBannerStatus | None = None


class DealerOfferCreate(PromotionFields):
    description: PromotionText | None = None
    scope_kind: OfferScopeKind
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    min_quantity: MinimumQuantity = Decimal("1")
    discount_percentage: DiscountPercentage
    is_active: bool = False

    @model_validator(mode="after")
    def matching_scope(self) -> DealerOfferCreate:
        actual = {
            name
            for name in ("category_id", "brand_id", "item_id")
            if getattr(self, name) is not None
        }
        expected = (
            {f"{self.scope_kind.lower()}_id"}
            if self.scope_kind is not OfferScopeKind.ALL
            else set()
        )
        if actual != expected:
            raise ValueError("Offer targets must match the selected scope exactly.")
        return self


class DealerOfferRead(DealerOfferCreate):
    id: uuid.UUID
    status: DealerBannerStatus


class DealerOfferUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: PromotionTitle | None = None
    description: PromotionText | None = None
    scope_kind: OfferScopeKind | None = None
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    audience_price_type: PriceType | None = None
    min_quantity: MinimumQuantity | None = None
    discount_percentage: DiscountPercentage | None = None
    is_active: bool | None = None
    sort_order: SortOrder | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class PromotionRetire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=REASON_MIN_LENGTH, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < REASON_MIN_LENGTH:
            raise ValueError(REASON_TOO_SHORT)
        return value


class DealerOfferPreview(BaseModel):
    item_count: int = Field(ge=0)
