"""The status lists that decide whether a vendor or a carrier may be retired.

``vendors.repository`` cannot import ``PurchaseOrderStatus`` or
``DispatchStatus``: master data sits below procurement and dispatch in
docs/architecture.md's order, so its open-document checks spell the statuses out
as strings. Both match their enums today. The risk is the next status: added to
the enum and to nothing here, it is simply absent from the open list - read as
closed - and a vendor with an order in that state, or a carrier with goods on
the road, could be retired.

So each enum is partitioned into the module's OPEN and CLOSED lists: every
status in exactly one of them, and no string the enum does not define. And a
status its state machine can never leave must be closed, or the record it names
could never be retired. The self-tests feed the checks each violation.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from src.modules.dispatch.models import DispatchStatus
from src.modules.dispatch.service import DISPATCH_LIFECYCLE
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.procurement.service_rules import PURCHASE_ORDER_LIFECYCLE
from src.modules.vendors.repository import (
    CLOSED_DISPATCH_STATUSES,
    CLOSED_PURCHASE_ORDER_STATUSES,
    OPEN_DISPATCH_STATUSES,
    OPEN_PURCHASE_ORDER_STATUSES,
)

pytestmark = pytest.mark.unit

PARTITIONS = [
    pytest.param(
        PurchaseOrderStatus,
        PURCHASE_ORDER_LIFECYCLE,
        OPEN_PURCHASE_ORDER_STATUSES,
        CLOSED_PURCHASE_ORDER_STATUSES,
        id="purchase-order",
    ),
    pytest.param(
        DispatchStatus,
        DISPATCH_LIFECYCLE,
        OPEN_DISPATCH_STATUSES,
        CLOSED_DISPATCH_STATUSES,
        id="dispatch",
    ),
]


def misplaced(
    vocabulary: Iterable[str], open_statuses: Iterable[str], closed_statuses: Iterable[str]
) -> list[str]:
    """Each way the two lists fail to partition ``vocabulary``, as a sentence."""
    known = set(vocabulary)
    opened, closed = list(open_statuses), list(closed_statuses)
    listed = opened + closed
    both = set(opened) & set(closed)
    repeated = {status for status in listed if listed.count(status) > 1} - both
    return [
        *(f"{status} is neither open nor closed" for status in sorted(known - set(listed))),
        *(f"{status} is both open and closed" for status in sorted(both)),
        *(f"{status} is listed twice" for status in sorted(repeated)),
        *(f"{status} is not a status" for status in sorted(set(listed) - known)),
    ]


def stuck_open(machine: Any, vocabulary: Iterable[str], open_statuses: Iterable[str]) -> list[str]:
    """Open statuses the state machine has no edge out of: a record that never retires."""
    sources = {str(transition.source) for transition in machine.transitions}
    return sorted(set(open_statuses) & (set(vocabulary) - sources))


@pytest.mark.parametrize(("statuses", "machine", "open_statuses", "closed_statuses"), PARTITIONS)
def test_every_status_is_open_or_closed_exactly_once(
    statuses: Any, machine: Any, open_statuses: tuple[str, ...], closed_statuses: tuple[str, ...]
) -> None:
    assert misplaced((str(status) for status in statuses), open_statuses, closed_statuses) == []


@pytest.mark.parametrize(("statuses", "machine", "open_statuses", "closed_statuses"), PARTITIONS)
def test_a_status_nothing_leaves_never_blocks_a_retirement(
    statuses: Any, machine: Any, open_statuses: tuple[str, ...], closed_statuses: tuple[str, ...]
) -> None:
    assert stuck_open(machine, (str(status) for status in statuses), open_statuses) == []


class TestTheChecksThemselves:
    """Each violation, fed to its check, is named; a clean partition passes."""

    def test_a_clean_partition_passes(self) -> None:
        assert misplaced(["A", "B", "C"], ["A"], ["B", "C"]) == []

    def test_a_status_in_neither_list_is_named(self) -> None:
        assert misplaced(["A", "B", "NEW"], ["A"], ["B"]) == ["NEW is neither open nor closed"]

    def test_a_status_in_both_lists_is_named(self) -> None:
        assert misplaced(["A", "B"], ["A", "B"], ["B"]) == ["B is both open and closed"]

    def test_a_status_listed_twice_is_named(self) -> None:
        assert misplaced(["A", "B"], ["A", "A"], ["B"]) == ["A is listed twice"]

    def test_a_string_the_enum_does_not_define_is_named(self) -> None:
        assert misplaced(["A", "B"], ["A", "TYPO"], ["B"]) == ["TYPO is not a status"]

    def test_an_open_status_nothing_leaves_is_named(self) -> None:
        machine = SimpleNamespace(transitions=[SimpleNamespace(source="A")])
        assert stuck_open(machine, ["A", "B", "C"], ["A", "B"]) == ["B"]
        assert stuck_open(machine, ["A", "B", "C"], ["A"]) == []
