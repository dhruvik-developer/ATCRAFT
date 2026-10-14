"""Dealer-portal core rules (WP C3a).

Pure predicates, no database:

* ``dealer_route_allowed``: the allow-list gate that keeps a dealer principal
  on the portal, its notifications and auth self-service.
* ``select_best_offer``: percentage only, no stacking, best single offer wins,
  with the documented tie-break (docs/assumptions.md §C3-3).
* ``availability_band``: live quantity to a band that never reveals a number
  (docs/assumptions.md §C3-2).
* ``assert_portal_role_consistency``: dealer roles only on dealer-bound
  accounts, and only dealer roles on them (docs/assumptions.md §C3-1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from src.core.deps import dealer_route_allowed
from src.modules.dealer_portal.models import AvailabilityBand
from src.modules.dealer_portal.pricing import (
    OfferFacts as Facts,
)
from src.modules.dealer_portal.pricing import (
    ancestor_sets,
    availability_band,
    select_best_offer,
)
from src.modules.users.service import assert_portal_role_consistency

pytestmark = pytest.mark.unit

PREFIX = "/api/v1"
ON = date(2026, 10, 14)


def _offer(**overrides) -> Facts:
    values: dict = {
        "id": uuid.uuid4(),
        "code": "OFFER",
        "title": "Offer",
        "scope_kind": "ALL",
        "category_id": None,
        "brand_id": None,
        "item_id": None,
        "audience_price_type": None,
        "customer_id": None,
        "min_quantity": Decimal("1"),
        "discount_percentage": Decimal("10"),
        "valid_from": None,
        "valid_to": None,
        "is_active": True,
        "sort_order": 0,
    }
    values.update(overrides)
    return Facts(**values)


@dataclass
class _Role:
    code: str
    permissions: list[str]
    is_vendor_role: bool = False
    is_dealer_role: bool = False
    is_active: bool = True
    deleted_at: None = None


INTERNAL = _Role("SALES_MANAGER", ["sales_order.view"])
DEALER = _Role("DEALER_OWNER", ["dealer_portal.browse"], is_dealer_role=True)
VENDOR = _Role("VENDOR_OWNER", ["vendor_portal.access"], is_vendor_role=True)


# -----------------------------------------------------------------------------
# The route allow-list
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"{PREFIX}/dealer-portal/me",
        f"{PREFIX}/dealer-portal/catalog/items",
        f"{PREFIX}/dealer-portal/cart",
        f"{PREFIX}/dealer-portal/orders/123/cancel",
        f"{PREFIX}/notifications",
        f"{PREFIX}/notifications/unread-count",
        f"{PREFIX}/auth/me",
        f"{PREFIX}/auth/logout",
        f"{PREFIX}/auth/logout-all",
        f"{PREFIX}/auth/change-password",
        f"{PREFIX}/auth/sessions",
        f"{PREFIX}/auth/mfa/setup",
    ],
)
def test_a_dealer_may_call_the_portal_notifications_and_self_service(path: str) -> None:
    assert dealer_route_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        f"{PREFIX}/sales/orders",
        f"{PREFIX}/dealer-portal-x/me",
        f"{PREFIX}/dealer-portalx",
        f"{PREFIX}/users",
        f"{PREFIX}/auth/login",
        f"{PREFIX}/auth/refresh",
        f"{PREFIX}/vendor-portal/me",
        f"{PREFIX}/products/items",
        f"{PREFIX}/finance/ledger",
        f"{PREFIX}/dispatch",
        "/api/v1",
    ],
)
def test_a_dealer_may_not_call_staff_or_sibling_portal_routes(path: str) -> None:
    assert not dealer_route_allowed(path)


# -----------------------------------------------------------------------------
# The best-offer rule
# -----------------------------------------------------------------------------


def _match(**overrides):
    return {
        "item_id": uuid.uuid4(),
        "category_ids": frozenset(),
        "brand_id": None,
        "quantity": Decimal("5"),
        "price_type": "DEALER",
        "customer_id": uuid.uuid4(),
        "on_date": ON,
        **overrides,
    }


def test_the_highest_percentage_wins() -> None:
    offers = [_offer(code="SMALL", discount_percentage=Decimal("5")), _offer(code="BIG")]
    assert select_best_offer(offers, **_match()).code == "BIG"  # type: ignore[arg-type]


def test_offers_do_not_stack() -> None:
    offers = [
        _offer(code="A", discount_percentage=Decimal("5")),
        _offer(code="B", discount_percentage=Decimal("7")),
    ]
    best = select_best_offer(offers, **_match())
    assert best is not None and best.discount_percentage == Decimal("7")


def test_inactive_expired_future_and_unreached_offers_are_ignored() -> None:
    item = uuid.uuid4()
    offers = [
        _offer(code="OFF", is_active=False, discount_percentage=Decimal("50")),
        _offer(code="PAST", valid_to=date(2026, 1, 1), discount_percentage=Decimal("50")),
        _offer(code="FUTURE", valid_from=date(2027, 1, 1), discount_percentage=Decimal("50")),
        _offer(code="BULK", min_quantity=Decimal("100"), discount_percentage=Decimal("50")),
        _offer(code="LIVE", discount_percentage=Decimal("3")),
    ]
    best = select_best_offer(offers, **_match(item_id=item))
    assert best is not None and best.code == "LIVE"


def test_scope_matching() -> None:
    item, brand, category, parent = (uuid.uuid4() for _ in range(4))
    ancestors = ancestor_sets([(category, parent), (parent, None)])
    offers = [
        _offer(code="ALL"),
        _offer(code="ITEM", scope_kind="ITEM", item_id=item),
        _offer(code="OTHER-ITEM", scope_kind="ITEM", item_id=uuid.uuid4()),
        _offer(code="BRAND", scope_kind="BRAND", brand_id=brand),
        _offer(code="CATEGORY", scope_kind="CATEGORY", category_id=parent),
    ]
    best = select_best_offer(
        offers, **_match(item_id=item, brand_id=brand, category_ids=ancestors[category])
    )
    # Same 10% everywhere: the item-scoped offer wins the specificity tie-break.
    assert best is not None and best.code == "ITEM"


def test_a_tie_breaks_toward_specificity_then_sort_order_then_id() -> None:
    item = uuid.uuid4()
    first, second = sorted([uuid.uuid4(), uuid.uuid4()], key=lambda value: value.int)
    offers = [
        _offer(code="GENERAL", scope_kind="ALL", id=first, sort_order=0),
        _offer(code="LATER", scope_kind="ALL", id=second, sort_order=0),
    ]
    assert select_best_offer(offers, **_match(item_id=item)).code == "GENERAL"  # type: ignore[union-attr]

    ordered = [
        _offer(code="SORTED-LAST", scope_kind="ALL", sort_order=5),
        _offer(code="SORTED-FIRST", scope_kind="ALL", sort_order=1),
    ]
    assert select_best_offer(ordered, **_match(item_id=item)).code == "SORTED-FIRST"  # type: ignore[union-attr]


def test_audience_and_negotiated_schemes() -> None:
    customer, stranger = uuid.uuid4(), uuid.uuid4()
    offers = [
        _offer(code="DIST", audience_price_type="DISTRIBUTOR", discount_percentage=Decimal("9")),
        _offer(code="MINE", customer_id=customer, discount_percentage=Decimal("8")),
        _offer(code="THEIRS", customer_id=stranger, discount_percentage=Decimal("30")),
    ]
    best = select_best_offer(offers, **_match(customer_id=customer))
    assert best is not None and best.code == "MINE"

    # Another dealer sees neither negotiated scheme; on the DISTRIBUTOR channel
    # the audience offer applies.
    assert select_best_offer(offers, **_match(customer_id=uuid.uuid4())) is None
    best = select_best_offer(offers, **_match(customer_id=uuid.uuid4(), price_type="DISTRIBUTOR"))
    assert best is not None and best.code == "DIST"


def test_no_applicable_offer_is_none() -> None:
    assert select_best_offer([], **_match()) is None  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# Availability bands
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "band"),
    [
        (Decimal("10"), AvailabilityBand.IN_STOCK),
        (Decimal("400"), AvailabilityBand.IN_STOCK),
        (Decimal("9.9999"), AvailabilityBand.LIMITED),
        (Decimal("0.0001"), AvailabilityBand.LIMITED),
        (Decimal("0"), AvailabilityBand.MADE_TO_ORDER),
    ],
)
def test_quantities_fold_into_bands(available: Decimal, band: AvailabilityBand) -> None:
    assert availability_band(available) is band


# -----------------------------------------------------------------------------
# Portal role consistency
# -----------------------------------------------------------------------------


def test_an_internal_account_may_not_hold_a_dealer_role() -> None:
    from src.core.errors import BusinessRuleError

    with pytest.raises(BusinessRuleError) as excinfo:
        assert_portal_role_consistency(None, None, [INTERNAL, DEALER])
    assert excinfo.value.details["dealer_roles"] == ["DEALER_OWNER"]


def test_a_dealer_account_may_hold_only_dealer_roles() -> None:
    from src.core.errors import BusinessRuleError

    customer = uuid.uuid4()
    with pytest.raises(BusinessRuleError) as excinfo:
        assert_portal_role_consistency(None, customer, [DEALER, INTERNAL])
    assert excinfo.value.details["customer_id"] == str(customer)

    assert_portal_role_consistency(None, customer, [DEALER])


def test_vendor_and_dealer_bindings_together_are_refused() -> None:
    from src.core.errors import BusinessRuleError

    with pytest.raises(BusinessRuleError):
        assert_portal_role_consistency(uuid.uuid4(), uuid.uuid4(), [VENDOR, DEALER])


def test_the_vendor_rule_still_holds_both_ways() -> None:
    from src.core.errors import BusinessRuleError
    from src.modules.users.service import assert_vendor_role_consistency

    with pytest.raises(BusinessRuleError) as excinfo:
        assert_vendor_role_consistency(None, [VENDOR])
    assert excinfo.value.details["vendor_roles"] == ["VENDOR_OWNER"]

    with pytest.raises(BusinessRuleError):
        assert_vendor_role_consistency(uuid.uuid4(), [INTERNAL])
    assert_vendor_role_consistency(uuid.uuid4(), [VENDOR])
