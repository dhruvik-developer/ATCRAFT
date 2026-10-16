from __future__ import annotations

import pytest

from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.sales_returns.models import SalesReturnStatus
from src.modules.sales_returns.service import SALES_RETURN_LIFECYCLE

pytestmark = pytest.mark.unit


def test_sales_return_has_only_the_documented_paths() -> None:
    assert SALES_RETURN_LIFECYCLE.can_transition(
        SalesReturnStatus.DRAFT, SalesReturnStatus.RECEIVED
    )
    assert SALES_RETURN_LIFECYCLE.can_transition(
        SalesReturnStatus.RECEIVED, SalesReturnStatus.CLOSED
    )
    assert SALES_RETURN_LIFECYCLE.can_transition(
        SalesReturnStatus.DRAFT, SalesReturnStatus.CANCELLED
    )
    assert not SALES_RETURN_LIFECYCLE.can_transition(
        SalesReturnStatus.RECEIVED, SalesReturnStatus.CANCELLED
    )


def test_sales_return_transitions_require_create_permission() -> None:
    actions = SALES_RETURN_LIFECYCLE.available_actions(
        SalesReturnStatus.DRAFT, {Permission.SALES_RETURN_CREATE}
    )
    assert [action.target for action in actions] == [
        SalesReturnStatus.RECEIVED,
        SalesReturnStatus.CANCELLED,
    ]


def test_sales_return_cannot_leave_terminal_state() -> None:
    with pytest.raises(InvalidStateTransitionError):
        SALES_RETURN_LIFECYCLE.assert_transition(SalesReturnStatus.CLOSED, SalesReturnStatus.DRAFT)
