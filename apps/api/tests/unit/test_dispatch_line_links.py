"""A split demand link must never move more stock than sales fulfilment records."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.errors import BusinessRuleError
from src.modules.dispatch.schemas import DispatchCreate, DispatchLineIn, DispatchUpdate
from src.modules.dispatch.service import _assert_unique_sales_order_lines


def _lines(*references: uuid.UUID | None) -> list[DispatchLineIn]:
    item_id = uuid.uuid4()
    return [
        DispatchLineIn(item_id=item_id, quantity=Decimal("40"), sales_order_line_id=ref)
        for ref in references
    ]


def test_create_refuses_two_rows_for_one_sales_order_line() -> None:
    line_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="same sales-order line"):
        DispatchCreate(
            warehouse_id=uuid.uuid4(),
            customer_id=uuid.uuid4(),
            dispatch_date=date(2026, 10, 14),
            lines=_lines(line_id, line_id),
        )


def test_replacing_lines_refuses_duplicate_demand_links() -> None:
    line_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="same sales-order line"):
        DispatchUpdate(lines=_lines(line_id, line_id))


def test_service_names_the_repeated_link_for_legacy_or_internal_payloads() -> None:
    line_id = uuid.uuid4()
    with pytest.raises(BusinessRuleError) as exc:
        _assert_unique_sales_order_lines(_lines(line_id, line_id))
    assert exc.value.details == {"sales_order_line_ids": [str(line_id)]}


def test_distinct_demands_and_unlinked_goods_remain_valid() -> None:
    lines = _lines(uuid.uuid4(), uuid.uuid4(), None, None)
    DispatchUpdate(lines=lines)
    _assert_unique_sales_order_lines(lines)
