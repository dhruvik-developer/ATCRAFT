"""Dealer-portal request and response contracts.

Read models name the fields a dealer may see. Staff notes, free stock
quantities, driver mobile numbers and freight cost are not among them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from src.modules.dealer_portal.models import AvailabilityBand, OfferScopeKind
from src.modules.sales.models import SalesOrderLineStatus, SalesOrderStatus

Reason = Field(min_length=3, max_length=500)


class DealerOfferSnippet(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    scope_kind: OfferScopeKind
    discount_percentage: Decimal
    min_quantity: Decimal


class DealerCreditPosition(BaseModel):
    credit_limit: Decimal | None
    credit_days: int
    outstanding_amount: Decimal
    order_value: Decimal
    exposure: Decimal
    headroom: Decimal | None
    is_unlimited: bool
    within_limit: bool
    is_blocked: bool
    failure_reason: str | None


class DealerAddressRead(BaseModel):
    id: uuid.UUID
    address_type: str
    label: str | None
    address_line1: str
    city: str
    state: str | None
    state_code: str | None
    postal_code: str | None
    is_default: bool


class DealerContactRead(BaseModel):
    name: str
    designation: str | None
    phone: str | None
    email: str | None


class DealerSalesPerson(BaseModel):
    """Name and phone only. The salesperson's email is not a dealer's to have."""

    name: str
    phone: str | None


class DealerPortalMe(BaseModel):
    customer_id: uuid.UUID
    customer_code: str
    customer_name: str
    customer_type: str
    price_list_type: str
    credit: DealerCreditPosition
    addresses: list[DealerAddressRead]
    primary_contact: DealerContactRead | None
    sales_person: DealerSalesPerson | None


class DealerCategoryNode(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    parent_id: uuid.UUID | None
    depth: int


class DealerBrandRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str


class DealerCatalogItem(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    category_id: uuid.UUID | None
    brand_id: uuid.UUID | None
    price: Decimal | None
    price_on_request: bool
    orderable: bool
    availability_band: AvailabilityBand
    best_offer: DealerOfferSnippet | None
    in_wishlist: bool
    in_cart_quantity: Decimal
    primary_image_id: uuid.UUID | None


class DealerSpecificationRead(BaseModel):
    name: str
    value: str
    unit: str | None


class DealerAccessoryRead(BaseModel):
    accessory_item_id: uuid.UUID
    code: str
    name: str
    relation: str
    quantity: Decimal
    #: OPTIONAL accessories carry a price and may be added to the cart.
    #: INCLUDED ones are listed as "In the box" and are not priced.
    price: Decimal | None
    price_on_request: bool
    in_the_box: bool


class DealerVariantRead(BaseModel):
    id: uuid.UUID
    code: str
    price: Decimal | None
    price_on_request: bool
    is_active: bool


class DealerPriceSlab(BaseModel):
    min_quantity: Decimal
    price: Decimal
    variant_id: uuid.UUID | None
    valid_from: date | None
    valid_to: date | None


class DealerCatalogItemDetail(DealerCatalogItem):
    description: str | None
    hsn_code: str | None
    gst_rate: Decimal
    can_return: bool
    variants: list[DealerVariantRead]
    slabs: list[DealerPriceSlab]
    offers: list[DealerOfferSnippet]
    image_ids: list[uuid.UUID]
    specifications: list[DealerSpecificationRead]
    accessories: list[DealerAccessoryRead]


class DealerPriceQuote(BaseModel):
    item_id: uuid.UUID
    variant_id: uuid.UUID | None
    quantity: Decimal
    gross_unit: Decimal | None
    offer: DealerOfferSnippet | None
    discount_percentage: Decimal
    net_unit: Decimal | None
    tax_rate: Decimal
    tax_estimate: Decimal | None
    price_on_request: bool


class DealerCartLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    item_variant_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0)


class DealerCartLineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Decimal = Field(gt=0)


class DealerCartWarning(BaseModel):
    code: str
    message: str
    line_id: uuid.UUID | None = None


class DealerCartLineRead(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    item_name: str
    item_variant_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal | None
    discount_percentage: Decimal
    offer: DealerOfferSnippet | None
    net_amount: Decimal | None
    tax_amount: Decimal | None
    line_total: Decimal | None
    price_on_request: bool
    warnings: list[DealerCartWarning]


class DealerCartRead(BaseModel):
    cart_id: uuid.UUID | None
    cart_version: int | None
    lines: list[DealerCartLineRead]
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    credit: DealerCreditPosition
    warnings: list[DealerCartWarning]


class DealerWishlistIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    item_variant_id: uuid.UUID | None = None


class DealerWishlistRead(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    item_code: str
    item_name: str
    item_variant_id: uuid.UUID | None
    price: Decimal | None
    price_on_request: bool
    availability_band: AvailabilityBand


class DealerCheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cart_version: int = Field(ge=1)
    required_date: date
    shipping_address_id: uuid.UUID | None = None
    billing_address_id: uuid.UUID | None = None
    customer_po_number: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=4000)


class DealerCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Reason


class DealerOrderFilters(BaseModel):
    search: str | None = None
    status: SalesOrderStatus | None = None
    open_only: bool = False


class DealerOrderLineRead(BaseModel):
    id: uuid.UUID
    line_number: int
    item_id: uuid.UUID
    item_code: str
    item_name: str
    item_variant_id: uuid.UUID | None
    quantity: Decimal
    unit_price: Decimal
    discount_percentage: Decimal
    offer_id: uuid.UUID | None
    tax_amount: Decimal
    line_total: Decimal
    allocated_quantity: Decimal
    dispatched_quantity: Decimal
    invoiced_quantity: Decimal
    #: Derived from the quantities, never stored - a line cannot claim
    #: ALLOCATED while its reserved quantity is short.
    allocation_status: SalesOrderLineStatus


class DealerOrderSummary(BaseModel):
    id: uuid.UUID
    code: str
    status: SalesOrderStatus
    order_date: date
    required_date: date
    currency: str
    total_amount: Decimal
    source: str
    line_count: int
    created_at: datetime


class DealerOrderDetail(DealerOrderSummary):
    customer_po_number: str | None
    subtotal: Decimal
    discount_amount: Decimal
    tax_amount: Decimal
    notes: str | None
    lines: list[DealerOrderLineRead]


class DealerTrackingEvent(BaseModel):
    event_status: str
    event_time: datetime
    location: str | None
    remark: str | None


class DealerDispatchSummary(BaseModel):
    id: uuid.UUID
    code: str
    status: str
    courier_name: str | None
    transport_name: str | None
    tracking_id: str | None
    lr_number: str | None
    dispatch_date: date
    dispatched_at: datetime | None
    expected_delivery_date: date | None
    actual_delivery_date: date | None


class DealerTracking(DealerDispatchSummary):
    """A consignment a dealer may see. Driver mobile and freight are absent."""

    events: list[DealerTrackingEvent]
    next_cursor: str | None


class DealerOverdueInvoice(BaseModel):
    """A placeholder row for a sales invoice that is past due.

    Sales invoices exist, but their portal outstanding projection is not yet
    implemented. This list stays empty; ledger-based ageing supplies the overdue
    position. ASSUMPTION: docs/assumptions.md §C3-10.
    """

    document_id: uuid.UUID
    document_code: str
    due_date: date | None
    outstanding_amount: Decimal
    currency: str


class DealerOutstanding(BaseModel):
    overdue_amount: Decimal
    currency: str
    ageing_total_outstanding: Decimal
    ageing_not_due: Decimal
    oldest_due_date: date | None
    open_document_count: int
    overdue_invoices: list[DealerOverdueInvoice]


__all__ = [
    "DealerAccessoryRead",
    "DealerAddressRead",
    "DealerBrandRead",
    "DealerCancelRequest",
    "DealerCartLineIn",
    "DealerCartLineRead",
    "DealerCartLineUpdate",
    "DealerCartRead",
    "DealerCartWarning",
    "DealerCatalogItem",
    "DealerCatalogItemDetail",
    "DealerCategoryNode",
    "DealerCheckoutRequest",
    "DealerContactRead",
    "DealerCreditPosition",
    "DealerDispatchSummary",
    "DealerOfferSnippet",
    "DealerOrderDetail",
    "DealerOrderFilters",
    "DealerOrderLineRead",
    "DealerOrderSummary",
    "DealerOutstanding",
    "DealerOverdueInvoice",
    "DealerPortalMe",
    "DealerPriceQuote",
    "DealerPriceSlab",
    "DealerSalesPerson",
    "DealerSpecificationRead",
    "DealerTracking",
    "DealerTrackingEvent",
    "DealerVariantRead",
    "DealerWishlistIn",
    "DealerWishlistRead",
]
