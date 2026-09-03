"""Price-list slab resolution.

Getting this wrong invoices a customer at the wrong rate, so the precedence is
pinned down here rather than left to whichever row the database returns first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from src.core.ids import new_id
from src.modules.products.models import PriceType
from src.modules.products.service import ITEM_MASTER_FALLBACK_FIELD, select_price_row

ITEM_ID = uuid.uuid4()
VARIANT_ID = uuid.uuid4()


@dataclass(frozen=True)
class PriceRow:
    """The subset of ``ItemPrice`` the resolver reads.

    Deliberately not an ORM instance: ``select_price_row`` is pure ranking
    logic, and a unit test for it should not need SQLAlchemy's mapper registry
    configured to run.
    """

    id: uuid.UUID
    variant_id: uuid.UUID | None
    price: Decimal
    min_quantity: Decimal
    valid_from: date


def price(
    *,
    amount: str,
    min_quantity: str = "0",
    valid_from: date = date(2026, 4, 1),
    variant_id: uuid.UUID | None = None,
) -> Any:
    return cast(
        Any,
        PriceRow(
            id=new_id(),
            variant_id=variant_id,
            price=Decimal(amount),
            min_quantity=Decimal(min_quantity),
            valid_from=valid_from,
        ),
    )


def test_base_slab_applies_below_the_next_break() -> None:
    rows = [price(amount="100", min_quantity="0"), price(amount="90", min_quantity="100")]
    chosen = select_price_row(rows, Decimal("50"))
    assert chosen is not None
    assert chosen.price == Decimal("100")


def test_highest_slab_the_quantity_reaches_wins() -> None:
    rows = [
        price(amount="100", min_quantity="0"),
        price(amount="90", min_quantity="100"),
        price(amount="85", min_quantity="500"),
    ]
    chosen = select_price_row(rows, Decimal("250"))
    assert chosen is not None
    assert chosen.price == Decimal("90")


def test_exact_slab_boundary_reaches_that_slab() -> None:
    rows = [price(amount="100", min_quantity="0"), price(amount="90", min_quantity="100")]
    chosen = select_price_row(rows, Decimal("100"))
    assert chosen is not None
    assert chosen.price == Decimal("90")


def test_a_slab_the_quantity_does_not_reach_is_never_applied() -> None:
    # The only row on file starts at 100 units; an order for 10 must not be
    # priced from it, and must fall through to the item-master fallback.
    rows = [price(amount="90", min_quantity="100")]
    assert select_price_row(rows, Decimal("10")) is None


def test_more_recent_effective_date_supersedes_its_predecessor() -> None:
    old = price(amount="100", valid_from=date(2026, 4, 1))
    new = price(amount="110", valid_from=date(2026, 7, 1))
    chosen = select_price_row([old, new], Decimal("5"))
    assert chosen is not None
    assert chosen.price == Decimal("110")


def test_variant_specific_row_beats_the_item_wide_row() -> None:
    generic = price(amount="100")
    specific = price(amount="130", variant_id=VARIANT_ID)
    chosen = select_price_row([generic, specific], Decimal("5"), variant_id=VARIANT_ID)
    assert chosen is not None
    assert chosen.variant_id == VARIANT_ID


def test_variant_specificity_outranks_a_deeper_item_wide_slab() -> None:
    # Specificity is the most significant key: a variant has its own price, and
    # a bulk slab on the parent item must not override it.
    generic_bulk = price(amount="80", min_quantity="100")
    specific_base = price(amount="130", min_quantity="0", variant_id=VARIANT_ID)
    chosen = select_price_row([generic_bulk, specific_base], Decimal("250"), variant_id=VARIANT_ID)
    assert chosen is not None
    assert chosen.price == Decimal("130")


def test_variant_row_is_ignored_when_pricing_the_plain_item() -> None:
    generic = price(amount="100")
    specific = price(amount="130", variant_id=VARIANT_ID)
    chosen = select_price_row([generic, specific], Decimal("5"), variant_id=None)
    assert chosen is not None
    assert chosen.variant_id is None


def test_empty_price_list_resolves_to_nothing() -> None:
    assert select_price_row([], Decimal("5")) is None


def test_distributor_has_no_item_master_fallback() -> None:
    # Deliberate: reusing the dealer price for a distributor would quietly
    # invoice the wrong party at the wrong rate.
    assert ITEM_MASTER_FALLBACK_FIELD[PriceType.DISTRIBUTOR] is None
    assert ITEM_MASTER_FALLBACK_FIELD[PriceType.SALE] == "sale_price"
    assert ITEM_MASTER_FALLBACK_FIELD[PriceType.DEALER] == "dealer_price"
