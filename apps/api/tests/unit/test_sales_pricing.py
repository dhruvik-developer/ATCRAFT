"""Price resolution for a sales order line.

Covers the three ways a line gets its price - a price-list slab, the item
master fallback, and an authorised override - and the authority rules attached
to the third. Pure: the price rows are constructed in memory, because the
ranking rule is a business rule and not a query.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from src.core.deps import Principal
from src.core.errors import BusinessRuleError, PermissionDeniedError
from src.core.permissions import Permission
from src.modules.dealer_portal.pricing import OfferFacts
from src.modules.products.models import ItemPrice, PriceType
from src.modules.sales.repository import SellableItem, VariantInfo
from src.modules.sales.schemas import SalesOrderLineIn
from src.modules.sales.service import (
    PricingContext,
    is_below_cost,
    price_line,
    resolve_unit_price,
)

pytestmark = pytest.mark.unit

COMPANY_ID = uuid.uuid4()
TODAY = date(2026, 6, 1)


def make_item(
    *,
    sale_price: Decimal = Decimal("100"),
    dealer_price: Decimal = Decimal("80"),
    standard_cost: Decimal = Decimal("60"),
    gst_rate: Decimal = Decimal("18"),
    item_id: uuid.UUID | None = None,
) -> SellableItem:
    return SellableItem(
        id=item_id or uuid.uuid4(),
        code="LOCK-001",
        name="Mortise Lock",
        base_uom_id=uuid.uuid4(),
        sale_uom_id=None,
        gst_rate=gst_rate,
        sale_price=sale_price,
        dealer_price=dealer_price,
        standard_cost=standard_cost,
        can_sell=True,
        is_active=True,
    )


def make_price(
    item: SellableItem,
    price: str,
    *,
    min_quantity: str = "0",
    valid_from: date = date(2026, 1, 1),
    variant_id: uuid.UUID | None = None,
    price_type: PriceType = PriceType.SALE,
) -> ItemPrice:
    """An unattached ``ItemPrice`` row.

    ``id`` is set explicitly: it is the deterministic tie-breaker in the ranking
    and is otherwise only populated at flush time.
    """
    row = ItemPrice(
        company_id=COMPANY_ID,
        item_id=item.id,
        variant_id=variant_id,
        price_type=price_type,
        currency="INR",
        price=Decimal(price),
        min_quantity=Decimal(min_quantity),
        valid_from=valid_from,
        is_active=True,
    )
    row.id = uuid.uuid4()
    return row


def principal_with(*permissions: Permission) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=COMPANY_ID,
        email="sales@atcraft.test",
        full_name="Sales Desk",
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
    )


def context_for(
    item: SellableItem,
    rows: list[ItemPrice],
    *,
    variants: dict[uuid.UUID, VariantInfo] | None = None,
    price_type: PriceType = PriceType.SALE,
    is_interstate: bool = True,
) -> PricingContext:
    return PricingContext(
        price_type=price_type,
        on_date=TODAY,
        is_interstate=is_interstate,
        items={item.id: item},
        variants=variants or {},
        price_rows={item.id: rows} if rows else {},
    )


# -----------------------------------------------------------------------------
# Quantity slabs
# -----------------------------------------------------------------------------


def test_highest_slab_the_quantity_reaches_wins():
    item = make_item()
    rows = [
        make_price(item, "100", min_quantity="0"),
        make_price(item, "90", min_quantity="50"),
        make_price(item, "80", min_quantity="100"),
    ]

    price, source = resolve_unit_price(
        item=item, variant=None, rows=rows, quantity=Decimal("120"), price_type=PriceType.SALE
    )

    assert price == Decimal("80.000000")
    assert source == "PRICE_LIST"


def test_a_slab_the_quantity_does_not_reach_is_not_a_candidate():
    """Ordering 10 must never be charged the 100-unit price."""
    item = make_item()
    rows = [
        make_price(item, "100", min_quantity="0"),
        make_price(item, "80", min_quantity="100"),
    ]

    price, _ = resolve_unit_price(
        item=item, variant=None, rows=rows, quantity=Decimal("10"), price_type=PriceType.SALE
    )

    assert price == Decimal("100.000000")


def test_quantity_exactly_on_the_slab_boundary_gets_the_slab_price():
    item = make_item()
    rows = [
        make_price(item, "100", min_quantity="0"),
        make_price(item, "90", min_quantity="50"),
    ]

    price, _ = resolve_unit_price(
        item=item, variant=None, rows=rows, quantity=Decimal("50"), price_type=PriceType.SALE
    )

    assert price == Decimal("90.000000")


def test_newer_validity_window_supersedes_its_predecessor_within_a_slab():
    item = make_item()
    rows = [
        make_price(item, "100", min_quantity="0", valid_from=date(2026, 1, 1)),
        make_price(item, "110", min_quantity="0", valid_from=date(2026, 4, 1)),
    ]

    price, _ = resolve_unit_price(
        item=item, variant=None, rows=rows, quantity=Decimal("5"), price_type=PriceType.SALE
    )

    assert price == Decimal("110.000000")


# -----------------------------------------------------------------------------
# Variants
# -----------------------------------------------------------------------------


def test_variant_specific_row_beats_an_item_wide_row_and_carries_no_delta():
    item = make_item()
    variant = VariantInfo(
        id=uuid.uuid4(),
        item_id=item.id,
        code="LOCK-001-BR",
        price_delta=Decimal("15"),
        is_active=True,
    )
    rows = [
        make_price(item, "100", min_quantity="0"),
        make_price(item, "125", min_quantity="0", variant_id=variant.id),
    ]

    price, _ = resolve_unit_price(
        item=item, variant=variant, rows=rows, quantity=Decimal("5"), price_type=PriceType.SALE
    )

    # The variant row is already the variant's price; adding the delta on top
    # would charge for the finish twice.
    assert price == Decimal("125.000000")


def test_item_wide_row_matched_for_a_variant_adds_the_variant_delta():
    item = make_item()
    variant = VariantInfo(
        id=uuid.uuid4(),
        item_id=item.id,
        code="LOCK-001-BR",
        price_delta=Decimal("15"),
        is_active=True,
    )
    rows = [make_price(item, "100", min_quantity="0")]

    price, _ = resolve_unit_price(
        item=item, variant=variant, rows=rows, quantity=Decimal("5"), price_type=PriceType.SALE
    )

    assert price == Decimal("115.000000")


# -----------------------------------------------------------------------------
# Item-master fallback
# -----------------------------------------------------------------------------


def test_no_price_row_falls_back_to_the_item_master_channel_column():
    item = make_item(sale_price=Decimal("101.50"), dealer_price=Decimal("88"))

    sale_price, sale_source = resolve_unit_price(
        item=item, variant=None, rows=[], quantity=Decimal("5"), price_type=PriceType.SALE
    )
    dealer_price, dealer_source = resolve_unit_price(
        item=item, variant=None, rows=[], quantity=Decimal("5"), price_type=PriceType.DEALER
    )

    assert (sale_price, sale_source) == (Decimal("101.500000"), "ITEM_MASTER")
    assert (dealer_price, dealer_source) == (Decimal("88.000000"), "ITEM_MASTER")


def test_distributor_channel_without_a_price_list_row_is_refused():
    """There is no distributor column on the item master, and quietly billing a
    distributor at the dealer rate would invoice the wrong party the wrong
    amount."""
    item = make_item()

    with pytest.raises(BusinessRuleError) as excinfo:
        resolve_unit_price(
            item=item,
            variant=None,
            rows=[],
            quantity=Decimal("5"),
            price_type=PriceType.DISTRIBUTOR,
        )

    assert "DISTRIBUTOR" in str(excinfo.value)


# -----------------------------------------------------------------------------
# Overrides and the below-cost rule
# -----------------------------------------------------------------------------


def test_override_requires_price_manage():
    item = make_item()
    payload = SalesOrderLineIn(
        line_number=1, item_id=item.id, quantity=Decimal("5"), unit_price=Decimal("95")
    )

    with pytest.raises(PermissionDeniedError):
        price_line(payload, context_for(item, []), principal_with(Permission.SALES_ORDER_CREATE))


def test_override_is_honoured_for_a_price_manager():
    item = make_item()
    payload = SalesOrderLineIn(
        line_number=1, item_id=item.id, quantity=Decimal("5"), unit_price=Decimal("95")
    )

    priced = price_line(payload, context_for(item, []), principal_with(Permission.PRICE_MANAGE))

    assert priced.unit_price == Decimal("95.000000")
    assert priced.price_source == "OVERRIDE"
    assert priced.is_price_override is True
    assert priced.is_below_cost is False


def test_override_below_standard_cost_is_flagged_for_approval():
    item = make_item(standard_cost=Decimal("60"))
    payload = SalesOrderLineIn(
        line_number=1, item_id=item.id, quantity=Decimal("5"), unit_price=Decimal("55")
    )

    priced = price_line(payload, context_for(item, []), principal_with(Permission.PRICE_MANAGE))

    assert priced.is_below_cost is True


def test_a_price_list_price_below_cost_is_not_treated_as_an_override():
    """Only an override triggers the approval. A price list that happens to sit
    under cost is a commercial decision somebody already made, on the price list
    screen, with its own permission."""
    item = make_item(standard_cost=Decimal("60"))
    rows = [make_price(item, "55", min_quantity="0")]
    payload = SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("5"))

    priced = price_line(
        payload, context_for(item, rows), principal_with(Permission.SALES_ORDER_CREATE)
    )

    assert priced.unit_price == Decimal("55.000000")
    assert priced.is_price_override is False
    assert priced.is_below_cost is False


def test_uncosted_item_never_counts_as_below_cost():
    assert is_below_cost(Decimal("1"), Decimal("0")) is False
    assert is_below_cost(Decimal("0"), Decimal("0")) is False
    assert is_below_cost(Decimal("59.99"), Decimal("60")) is True
    assert is_below_cost(Decimal("60"), Decimal("60")) is False


# -----------------------------------------------------------------------------
# Dealer schemes as the default discount (docs/assumptions.md §C3-3)
# -----------------------------------------------------------------------------


def _scheme(percentage: str) -> OfferFacts:
    return OfferFacts(
        id=uuid.uuid4(),
        code=f"SCHEME-{percentage}",
        title="Scheme",
        scope_kind="ALL",
        category_id=None,
        brand_id=None,
        item_id=None,
        audience_price_type=None,
        customer_id=None,
        min_quantity=Decimal("1"),
        discount_percentage=Decimal(percentage),
        valid_from=None,
        valid_to=None,
        is_active=True,
        sort_order=0,
    )


def test_a_scheme_is_the_default_discount_when_none_is_given():
    from dataclasses import replace

    item = make_item()
    offer = _scheme("10")
    context = replace(context_for(item, []), offers=(offer,))
    payload = SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("2"))

    priced = price_line(payload, context, principal_with(Permission.SALES_ORDER_CREATE))

    assert priced.discount_percentage == Decimal("10")
    assert priced.offer_id == offer.id
    assert priced.amounts.discount_amount == Decimal("20.00")


def test_an_explicit_discount_wins_and_carries_no_scheme():
    from dataclasses import replace

    item = make_item()
    offer = _scheme("10")
    context = replace(context_for(item, []), offers=(offer,))
    payload = SalesOrderLineIn(
        line_number=1, item_id=item.id, quantity=Decimal("2"), discount_percentage=Decimal("5")
    )

    priced = price_line(payload, context, principal_with(Permission.SALES_ORDER_CREATE))

    assert priced.discount_percentage == Decimal("5")
    assert priced.offer_id is None
    assert priced.amounts.discount_amount == Decimal("10.00")


def test_no_schemes_means_no_default_discount():
    item = make_item()
    payload = SalesOrderLineIn(line_number=1, item_id=item.id, quantity=Decimal("2"))

    priced = price_line(
        payload, context_for(item, []), principal_with(Permission.SALES_ORDER_CREATE)
    )

    assert priced.discount_percentage == Decimal("0")
    assert priced.offer_id is None
    assert priced.amounts.discount_amount == Decimal("0.00")


# -----------------------------------------------------------------------------
# Whole-line pricing
# -----------------------------------------------------------------------------


def test_priced_line_carries_the_item_tax_rate_and_computed_amounts():
    item = make_item(gst_rate=Decimal("18"))
    rows = [make_price(item, "100", min_quantity="0"), make_price(item, "90", min_quantity="10")]
    payload = SalesOrderLineIn(
        line_number=1,
        item_id=item.id,
        quantity=Decimal("10"),
        discount_percentage=Decimal("5"),
    )

    priced = price_line(
        payload,
        context_for(item, rows, is_interstate=False),
        principal_with(Permission.SALES_ORDER_CREATE),
    )

    assert priced.unit_price == Decimal("90.000000")
    assert priced.tax_rate == Decimal("18")
    assert priced.amounts.gross_amount == Decimal("900.00")
    assert priced.amounts.discount_amount == Decimal("45.00")
    assert priced.amounts.taxable_amount == Decimal("855.00")
    assert priced.amounts.cgst_amount == priced.amounts.sgst_amount == Decimal("76.95")
    assert priced.amounts.line_total == Decimal("1008.90")
    # The description defaults to the item name so the document prints something.
    assert priced.description == item.name
    assert priced.uom_id == item.base_uom_id
