"""The owner registry quality calls back when it decides an inspection (WP E4).

Pure: no database. What is pinned here is the *wiring* - every binding names a
function its module really has, the reference types that must reach an owner do,
the ones that must not do not - and the verdict object's arithmetic. The
behaviour each owner applies is exercised against PostgreSQL in
``tests/integration/test_quality_owner_callbacks.py`` and
``test_assembly_completion.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from src.modules.quality import owners
from src.modules.quality.models import InspectionResult, InspectionStatus, QcInspection, QcStage

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_resolution() -> Any:
    """``resolve_owner`` caches per process; each test starts from the table."""
    owners.resolve_owner.cache_clear()
    yield
    owners.resolve_owner.cache_clear()


def _inspection(**quantities: str) -> QcInspection:
    """A transient inspection row - never flushed, only read."""
    decided = any(Decimal(value) > 0 for value in quantities.values())
    return QcInspection(
        id=uuid.uuid4(),
        code="QC/2026-27/00042",
        qc_stage=QcStage.ASSEMBLY,
        item_id=uuid.uuid4(),
        warehouse_id=uuid.uuid4(),
        reference_type="assembly_order",
        reference_id=uuid.uuid4(),
        reference_code="AO/2026-27/00007",
        inspected_quantity=Decimal("10"),
        sample_size=Decimal("10"),
        accepted_quantity=Decimal(quantities.get("accepted", "0")),
        rejected_quantity=Decimal(quantities.get("rejected", "0")),
        rework_quantity=Decimal(quantities.get("rework", "0")),
        hold_quantity=Decimal(quantities.get("hold", "0")),
        scrap_quantity=Decimal(quantities.get("scrap", "0")),
        result=InspectionResult.ACCEPTED if decided else InspectionResult.PENDING,
        status=InspectionStatus.ACCEPTED if decided else InspectionStatus.PENDING,
    )


# -----------------------------------------------------------------------------
# The table
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference_type",
    ["goods_receipt", "assembly_order", "job_work_receipt", "job_work_order"],
)
def test_every_owner_of_held_stock_is_called_back(reference_type: str) -> None:
    hooks = owners.resolve_owner(reference_type)
    assert hooks is not None
    assert callable(hooks.handler)
    # The lock is what keeps a decision's lock order the owner's own.
    assert callable(hooks.lock)
    assert callable(hooks.awaiting)


def test_every_binding_names_functions_its_module_really_has() -> None:
    """A renamed owner function must fail here, not strand documents in production."""
    for reference_type in owners.OWNER_BINDINGS:
        owners.resolve_owner(reference_type)


def test_a_job_work_lot_reaches_one_owner_under_both_of_its_keys() -> None:
    receipt = owners.resolve_owner("job_work_receipt")
    legacy = owners.resolve_owner("job_work_order")
    assert receipt is not None and legacy is not None
    assert receipt.handler is legacy.handler


def test_production_bookings_and_operations_share_one_owner_and_returns_are_called_back() -> None:
    # WP E5 wired the placeholder: held production output reports its verdict
    # through the same handler whether the inspection names the booking or the
    # operation, and the sweep walks that owner once for both keys.
    entry = owners.resolve_owner("production_entry")
    operation = owners.resolve_owner("work_order")
    assert entry is not None and operation is not None
    assert entry.handler is operation.handler
    assert entry.handler is not None
    assert entry.lock is operation.lock
    assert entry.awaiting is operation.awaiting
    returns = owners.resolve_owner("sales_return")
    assert returns is not None
    assert callable(returns.handler)
    assert callable(returns.lock)
    assert callable(returns.awaiting)
    assert owners.resolve_owner("manual") is None


def test_a_module_that_is_not_deployed_is_skipped(monkeypatch) -> None:
    monkeypatch.setitem(
        owners.OWNER_BINDINGS,
        "absent_document",
        owners.OwnerBinding(module="src.modules.not_built_yet.service", handler="apply"),
    )
    assert owners.resolve_owner("absent_document") is None


def test_a_binding_naming_a_missing_function_fails_loudly(monkeypatch) -> None:
    monkeypatch.setitem(
        owners.OWNER_BINDINGS,
        "misnamed",
        owners.OwnerBinding(module="src.modules.assembly.service", handler="no_such_handler"),
    )
    with pytest.raises(RuntimeError, match="no_such_handler"):
        owners.resolve_owner("misnamed")


def test_the_sweep_walks_a_shared_owner_once() -> None:
    hooks = owners._awaiting_hooks()
    # Goods receipts, assembly orders, job-work lots, production bookings and
    # customer returns: five owners, although job work and production are each
    # bound under two reference types.
    assert len(hooks) == 5
    assert len({id(hook) for hook in hooks}) == 5


# -----------------------------------------------------------------------------
# The verdict
# -----------------------------------------------------------------------------


def test_a_verdict_carries_the_cumulative_disposition() -> None:
    verdict = owners.QualityVerdict.of(
        _inspection(accepted="7", rejected="2", scrap="1"), reason="Key test"
    )
    assert verdict.accepted_quantity == Decimal("7.0000")
    assert verdict.refused_quantity == Decimal("3.0000")
    assert verdict.still_held_quantity == 0
    assert verdict.is_settled
    assert verdict.status == "ACCEPTED"
    assert verdict.reason == "Key test"


def test_a_verdict_with_quantity_left_on_hold_is_not_settled() -> None:
    assert not owners.QualityVerdict.of(
        _inspection(accepted="6", rework="4"), reason="r"
    ).is_settled
    assert not owners.QualityVerdict.of(_inspection(accepted="6", hold="4"), reason="r").is_settled


def test_an_undecided_inspection_is_not_settled() -> None:
    assert not owners.QualityVerdict.of(_inspection(), reason="r").is_settled


# -----------------------------------------------------------------------------
# Notifying
# -----------------------------------------------------------------------------


async def test_the_owner_is_told_the_verdict_and_its_answer_is_returned(monkeypatch) -> None:
    told: list[owners.QualityVerdict] = []

    async def handler(session: Any, tenant: Any, verdict: owners.QualityVerdict) -> bool:
        told.append(verdict)
        return True

    monkeypatch.setattr(
        owners,
        "resolve_owner",
        lambda _reference_type: owners.OwnerHooks(handler=handler, lock=None, awaiting=None),
    )
    inspection = _inspection(accepted="10")

    changed = await owners.notify_owner(None, None, inspection, reason="All pass")  # type: ignore[arg-type]

    assert changed is True
    assert [verdict.inspection_id for verdict in told] == [inspection.id]
    assert told[0].reference_id == inspection.reference_id


async def test_an_inspection_naming_no_document_tells_nobody(monkeypatch) -> None:
    called: list[object] = []

    async def handler(*args: object) -> bool:
        called.append(args)
        return True

    monkeypatch.setattr(
        owners,
        "resolve_owner",
        lambda _reference_type: owners.OwnerHooks(handler=handler, lock=None, awaiting=None),
    )
    inspection = _inspection(accepted="10")
    inspection.reference_id = None

    assert await owners.notify_owner(None, None, inspection, reason="r") is False  # type: ignore[arg-type]
    assert called == []


async def test_a_failing_owner_is_not_swallowed(monkeypatch) -> None:
    async def handler(*_: object) -> bool:
        raise ValueError("owner refused")

    monkeypatch.setattr(
        owners,
        "resolve_owner",
        lambda _reference_type: owners.OwnerHooks(handler=handler, lock=None, awaiting=None),
    )
    with pytest.raises(ValueError, match="owner refused"):
        await owners.notify_owner(None, None, _inspection(accepted="10"), reason="r")  # type: ignore[arg-type]
