"""Planning's conversion hook creates job work from explicit planner inputs."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ValidationError
from src.modules.job_work import service
from src.modules.job_work.models import JobWorkProcess, JobWorkSourceType
from src.modules.job_work.schemas import JobWorkOrderCreate
from src.modules.mrp.schemas import ConversionTarget
from src.modules.mrp.service import CONVERSION_HOOKS

pytestmark = pytest.mark.unit


def _request(**overrides: Any) -> Any:
    values = {
        "suggestion_id": uuid.uuid4(),
        "item_id": uuid.uuid4(),
        "item_code": "PLT-01",
        "quantity": Decimal("40"),
        "required_date": date(2026, 10, 1),
        "suggested_order_date": date(2026, 9, 10),
        "warehouse_ids": [],
        "vendor_id": uuid.uuid4(),
        "process_type": JobWorkProcess.PVD.value,
        "expected_return_date": date(2026, 10, 15),
        "warehouse_id": uuid.uuid4(),
        "notes": "MRP shortage",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_the_hook_is_the_one_planning_resolves() -> None:
    module_name, function_name = CONVERSION_HOOKS[ConversionTarget.JOB_WORK_ORDER]
    assert module_name == service.__name__
    assert getattr(service, function_name) is service.create_order_from_mrp


async def test_missing_planner_inputs_are_reported_before_database_access() -> None:
    request = _request(
        vendor_id=None, process_type=None, expected_return_date=None, warehouse_id=None
    )
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())

    with pytest.raises(BusinessRuleError) as exc:
        await service.create_order_from_mrp(object(), tenant, request)  # type: ignore[arg-type]

    assert exc.value.message == service.MRP_CONVERSION_INPUTS_REQUIRED
    assert exc.value.details["missing_fields"] == [
        "vendor_id",
        "process_type",
        "expected_return_date",
        "warehouse_id",
    ]


async def test_the_hook_uses_the_normal_order_creation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    order = SimpleNamespace(id=uuid.uuid4(), code="JWO-0001")
    captured: list[JobWorkOrderCreate] = []

    async def create_order(session: Any, actual_tenant: Any, payload: JobWorkOrderCreate) -> Any:
        assert session == "session"
        assert actual_tenant is tenant
        captured.append(payload)
        return order, []

    monkeypatch.setattr(service, "create_order", create_order)

    result = await service.create_order_from_mrp("session", tenant, request)

    assert result == (order.id, order.code)
    assert len(captured) == 1
    payload = captured[0]
    assert payload.vendor_id == request.vendor_id
    assert payload.process_type is JobWorkProcess.PVD
    assert payload.expected_return_date == request.expected_return_date
    assert payload.warehouse_id == request.warehouse_id
    assert payload.source_type is JobWorkSourceType.MRP_SUGGESTION
    assert payload.source_id == request.suggestion_id
    assert payload.lines[0].item_id == request.item_id
    assert payload.lines[0].quantity_to_issue == request.quantity
    assert payload.notes == request.notes


async def test_an_unsupported_process_is_rejected_before_creating_an_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_create_order(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unsupported process must be rejected before order creation")

    monkeypatch.setattr(service, "create_order", unexpected_create_order)

    with pytest.raises(ValidationError, match="process is not supported"):
        await service.create_order_from_mrp(
            object(),  # type: ignore[arg-type]
            TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4()),
            _request(process_type="UNKNOWN"),
        )


async def test_the_issuing_warehouse_must_match_the_run_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_create_order(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("out-of-scope warehouse must be rejected before order creation")

    monkeypatch.setattr(service, "create_order", unexpected_create_order)
    allowed_warehouse = uuid.uuid4()

    with pytest.raises(ValidationError, match="included in the MRP run's warehouse scope"):
        await service.create_order_from_mrp(
            object(),  # type: ignore[arg-type]
            TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4()),
            _request(warehouse_ids=[allowed_warehouse]),
        )
