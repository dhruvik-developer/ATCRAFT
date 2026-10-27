"""Planning's conversion hook into procurement.

``mrp.service.CONVERSION_HOOKS`` names ``create_requisition_from_mrp`` as the
document factory for every PURCHASE suggestion - the default target - and for
a long time nothing by that name existed, so every conversion closed the
suggestion with "module not available" and created nothing.

The hook is exercised here with the two database reads it makes replaced: the
item facts it loads, and the requisition body it delegates to. What is pinned
is the *shape* of the requisition planning raises - its source, its warehouse
resolution, its single line - and the refusal when no warehouse can be found.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.procurement import service
from src.modules.procurement.models import PurchaseSourceType
from src.modules.procurement.repository import PurchasableItem
from src.modules.procurement.schemas import RequisitionCreate

pytestmark = pytest.mark.unit


def _item(item_id: uuid.UUID, *, default_warehouse_id: uuid.UUID | None) -> PurchasableItem:
    return PurchasableItem(
        id=item_id,
        code="RM-001",
        name="Raw casting",
        base_uom_id=uuid.uuid4(),
        purchase_uom_id=None,
        gst_rate=Decimal("18"),
        standard_cost=Decimal("10"),
        min_order_quantity=Decimal("0"),
        lead_time_days=7,
        can_purchase=True,
        is_active=True,
        is_batch_tracked=False,
        is_serial_tracked=False,
        default_warehouse_id=default_warehouse_id,
    )


def _request(item_id: uuid.UUID, *, warehouse_ids: list[uuid.UUID]) -> Any:
    return SimpleNamespace(
        suggestion_id=uuid.uuid4(),
        item_id=item_id,
        item_code="RM-001",
        quantity=Decimal("25.5"),
        required_date=date(2026, 10, 1),
        suggested_order_date=date(2026, 9, 10),
        warehouse_ids=warehouse_ids,
        source_demands=[
            {
                "source": "SALES_ORDER",
                "source_id": str(uuid.uuid4()),
                "source_line_id": str(uuid.uuid4()),
            }
        ],
        notes="Shortage on SO-0007",
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the two reads: item facts, and the requisition body."""
    state: dict[str, Any] = {}

    async def load(self: Any, item_ids: Any) -> dict[uuid.UUID, PurchasableItem]:
        return state["items"]

    async def create(
        session: Any,
        tenant: Any,
        payload: RequisitionCreate,
        *,
        department_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
    ) -> tuple[Any, list[Any]]:
        state["payload"] = payload
        state["scope"] = (department_id, branch_id)
        return SimpleNamespace(id=state["created_id"], code="PR-0042"), []

    monkeypatch.setattr(service.PurchasableItemRepository, "load", load)
    monkeypatch.setattr(service, "_create_requisition", create)
    state["created_id"] = uuid.uuid4()
    return state


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


async def test_the_hook_raises_a_requisition_sourced_from_the_suggestion(
    captured: dict[str, Any],
) -> None:
    item_id = uuid.uuid4()
    run_warehouse = uuid.uuid4()
    captured["items"] = {item_id: _item(item_id, default_warehouse_id=uuid.uuid4())}
    request = _request(item_id, warehouse_ids=[run_warehouse])

    created = await service.create_requisition_from_mrp(None, _tenant(), request)  # type: ignore[arg-type]

    assert created == (captured["created_id"], "PR-0042")
    payload: RequisitionCreate = captured["payload"]
    assert payload.source_type is PurchaseSourceType.MRP_SUGGESTION
    assert payload.source_id == request.suggestion_id
    # The run's first warehouse wins over the item's default.
    assert payload.warehouse_id == run_warehouse
    assert payload.required_date == date(2026, 10, 1)
    assert payload.notes == "Shortage on SO-0007"
    assert len(payload.lines) == 1
    line = payload.lines[0]
    assert (line.line_number, line.item_id) == (1, item_id)
    assert line.quantity == Decimal("25.5000")
    assert line.required_date == date(2026, 10, 1)
    assert line.source_demand_refs[0].model_dump(mode="json") == {
        "demand_type": "SALES_ORDER",
        "demand_id": request.source_demands[0]["source_id"],
        "demand_line_id": request.source_demands[0]["source_line_id"],
    }
    # Planning carries no principal, so no department or branch is assumed.
    assert captured["scope"] == (None, None)


async def test_the_items_default_warehouse_is_used_when_the_run_named_none(
    captured: dict[str, Any],
) -> None:
    item_id = uuid.uuid4()
    default_warehouse = uuid.uuid4()
    captured["items"] = {item_id: _item(item_id, default_warehouse_id=default_warehouse)}

    await service.create_requisition_from_mrp(
        None,  # type: ignore[arg-type]
        _tenant(),
        _request(item_id, warehouse_ids=[]),
    )

    assert captured["payload"].warehouse_id == default_warehouse


async def test_no_warehouse_anywhere_is_refused_with_the_item_named(
    captured: dict[str, Any],
) -> None:
    """Never a silently invented destination (CLAUDE.md: no invented rules)."""
    item_id = uuid.uuid4()
    captured["items"] = {item_id: _item(item_id, default_warehouse_id=None)}

    with pytest.raises(BusinessRuleError, match="RM-001 has no default warehouse"):
        await service.create_requisition_from_mrp(
            None,  # type: ignore[arg-type]
            _tenant(),
            _request(item_id, warehouse_ids=[]),
        )
    assert "payload" not in captured


async def test_an_unknown_item_is_not_found(captured: dict[str, Any]) -> None:
    captured["items"] = {}
    with pytest.raises(NotFoundError):
        await service.create_requisition_from_mrp(
            None,  # type: ignore[arg-type]
            _tenant(),
            _request(uuid.uuid4(), warehouse_ids=[]),
        )


def test_the_hook_is_exported_under_the_name_planning_resolves() -> None:
    from src.modules.mrp.schemas import ConversionTarget
    from src.modules.mrp.service import CONVERSION_HOOKS

    module_name, function_name = CONVERSION_HOOKS[ConversionTarget.PURCHASE_REQUISITION]
    assert module_name == service.__name__
    assert function_name in service.__all__
    assert getattr(service, function_name) is service.create_requisition_from_mrp


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [{"source": "BOM", "via_item_id": str(uuid.uuid4())}],
        [{"root_demand_refs": [], "root_demand_refs_complete": True}],
        [{"root_demand_refs": [], "root_demand_refs_complete": False}],
    ],
)
async def test_incomplete_links_refuse_before_any_item_read_or_document_creation(
    captured: dict[str, Any], sources: list[dict[str, Any]]
) -> None:
    request = _request(uuid.uuid4(), warehouse_ids=[])
    request.source_demands = sources
    # No item fixtures: reaching a database read would itself fail this test.
    with pytest.raises(BusinessRuleError, match=r"(?i)(demand|provenance)"):
        await service.create_requisition_from_mrp(None, _tenant(), request)  # type: ignore[arg-type]
    assert "payload" not in captured
