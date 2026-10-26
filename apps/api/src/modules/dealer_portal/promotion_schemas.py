"""Validated promotion content; scope targets and date windows stay consistent."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.modules.dealer_portal.models import (
    DealerBannerLinkKind,
    DealerBannerStatus,
    OfferScopeKind,
)
from src.modules.products.models import PriceType


class PromotionFields(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    code: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_/-]*$")
    title: str = Field(min_length=1, max_length=160)
    audience_price_type: PriceType | None = None
    sort_order: int = Field(default=0, ge=0, le=100000)
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
    subtitle: str | None = Field(default=None, max_length=2000)
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

    title: str | None = None
    subtitle: str | None = None
    image_document_id: uuid.UUID | None = None
    link_kind: DealerBannerLinkKind | None = None
    link_id: uuid.UUID | None = None
    audience_price_type: PriceType | None = None
    sort_order: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    status: DealerBannerStatus | None = None


class DealerOfferCreate(PromotionFields):
    description: str | None = Field(default=None, max_length=2000)
    scope_kind: OfferScopeKind
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    min_quantity: Decimal = Field(default=Decimal("1"), gt=0, max_digits=18, decimal_places=4)
    discount_percentage: Decimal = Field(gt=0, le=100, max_digits=9, decimal_places=4)
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

    title: str | None = None
    description: str | None = None
    scope_kind: OfferScopeKind | None = None
    category_id: uuid.UUID | None = None
    brand_id: uuid.UUID | None = None
    item_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    audience_price_type: PriceType | None = None
    min_quantity: Decimal | None = None
    discount_percentage: Decimal | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None


class PromotionRetire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Enter a meaningful reason of at least three characters.")
        return value


class DealerOfferPreview(BaseModel):
    item_count: int = Field(ge=0)
