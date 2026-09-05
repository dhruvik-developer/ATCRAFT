"""Three seams in the MRP service, each pinned without a database.

* **The status URL follows the mounted prefix.** ``STATUS_URL_TEMPLATE`` is the
  only place a client is told where to poll; a literal ``/api/v1`` would point
  at a route that stops existing the day ``api_v1_prefix`` changes.

* **A failure to write the FAILED record is logged.** When ``fail_run`` itself
  fails, the run stays RUNNING for ``RUN_STALE_AFTER_SECONDS`` and every
  ``start_run`` answers 409. The original error is re-raised; the reason the
  record could not be written must reach the log, not ``contextlib.suppress``.

* **A deployed module without a hook is a refusal, not a closed suggestion.**
  ``CONVERSION_HOOKS`` named ``procurement.create_requisition_from_mrp`` and
  ``job_work.create_order_from_mrp`` before either existed, and a present
  module lacking the function was treated like an absent module: every purchase
  suggestion was marked CONVERTED with "module not available" and no
  requisition. The procurement hook now exists; job work cannot fabricate a
  vendor, so it is refused with the reason and the suggestion stays open.
"""

from __future__ import annotations

import importlib
import types
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.core.config import settings
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, NotFoundError
from src.modules.mrp import service
from src.modules.mrp.repository import (
    MRP_RUN_ENTITY,
    MRP_SUGGESTION_ENTITY,
    MrpRunRepository,
    MrpSuggestionRepository,
)
from src.modules.mrp.schemas import ConversionRequest, ConversionTarget

pytestmark = pytest.mark.unit


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _request(target: ConversionTarget) -> ConversionRequest:
    return ConversionRequest(
        suggestion_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        target=target,
        item_id=uuid.uuid4(),
        item_code="CAST-01",
        quantity=Decimal("25"),
        required_date=date(2026, 10, 1),
        suggested_order_date=date(2026, 9, 10),
    )


# -----------------------------------------------------------------------------
# Status URL
# -----------------------------------------------------------------------------


def test_the_status_url_is_built_from_the_mounted_prefix() -> None:
    prefix = settings.api_v1_prefix.rstrip("/")
    run_id = uuid.uuid4()

    # The template is prefix-relative; the prefix is applied when the URL is built.
    assert service.STATUS_URL_TEMPLATE == "/mrp/runs/{run_id}"
    assert service.status_url(run_id) == f"{prefix}/mrp/runs/{run_id}"
    assert service.status_url(run_id).startswith(prefix + "/")


def test_the_status_url_reads_the_prefix_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """An import-time f-string over ``settings`` ignored a prefix bound later."""
    run_id = uuid.uuid4()
    before = service.status_url(run_id)
    monkeypatch.setattr(settings, "api_v1_prefix", "/elsewhere/")
    assert service.status_url(run_id) == f"/elsewhere/mrp/runs/{run_id}"
    assert service.status_url(run_id) != before


# -----------------------------------------------------------------------------
# Inline runs: the failure to record a failure is logged
# -----------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.exceptions: list[tuple[str, dict[str, Any]]] = []

    def exception(self, event: str, **fields: Any) -> None:
        self.exceptions.append((event, fields))


class _Untouchable:
    def __getattr__(self, name: str) -> Any:  # pragma: no cover - failure path
        raise AssertionError(f"session.{name} was touched")


class CalculationError(RuntimeError):
    pass


class RecordingError(RuntimeError):
    pass


async def test_a_failed_fail_run_is_logged_and_the_original_error_still_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid.uuid4()
    log = _Log()

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise CalculationError("statement timeout")

    async def cannot_record(*args: Any, **kwargs: Any) -> None:
        raise RecordingError("lock timeout")

    monkeypatch.setattr(service, "execute_run", explode)
    monkeypatch.setattr(service, "fail_run", cannot_record)
    monkeypatch.setattr(service, "log", log)

    with pytest.raises(CalculationError):
        await service._run_inline(_Untouchable(), _tenant(), run_id)  # type: ignore[arg-type]

    assert log.exceptions == [("mrp.fail_run_failed", {"run_id": str(run_id)})]


async def test_a_successful_fail_run_logs_nothing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _Log()
    recorded: list[str] = []

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise CalculationError("boom")

    async def record(session: Any, tenant: Any, *, run_id: uuid.UUID, error: str) -> None:
        recorded.append(error)

    monkeypatch.setattr(service, "execute_run", explode)
    monkeypatch.setattr(service, "fail_run", record)
    monkeypatch.setattr(service, "log", log)

    with pytest.raises(CalculationError):
        await service._run_inline(_Untouchable(), _tenant(), uuid.uuid4())  # type: ignore[arg-type]

    assert recorded and log.exceptions == []


# -----------------------------------------------------------------------------
# Conversion hooks
# -----------------------------------------------------------------------------


def _fake_import(modules: dict[str, Any]) -> Any:
    def import_module(name: str) -> Any:
        try:
            return modules[name]
        except KeyError:
            raise ModuleNotFoundError(name) from None

    return import_module


async def test_an_absent_module_still_returns_none_for_the_caller_to_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service.importlib, "import_module", _fake_import({}))
    target = ConversionTarget.PURCHASE_REQUISITION
    assert (
        await service._invoke_conversion_hook(
            _Untouchable(),  # type: ignore[arg-type]
            _tenant(),
            target,
            _request(target),
        )
        is None
    )


async def test_a_present_module_without_the_hook_refuses_instead_of_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ConversionTarget.JOB_WORK_ORDER
    module_name, function_name = service.CONVERSION_HOOKS[target]
    bare = types.ModuleType(module_name)
    assert not hasattr(bare, function_name)
    monkeypatch.setattr(service.importlib, "import_module", _fake_import({module_name: bare}))

    with pytest.raises(BusinessRuleError, match="needs a vendor and a process") as excinfo:
        await service._invoke_conversion_hook(
            _Untouchable(),  # type: ignore[arg-type]
            _tenant(),
            target,
            _request(target),
        )
    assert excinfo.value.details == {"target": target.value, "item_code": "CAST-01"}


async def test_a_present_hook_is_called_and_its_document_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ConversionTarget.PURCHASE_REQUISITION
    module_name, function_name = service.CONVERSION_HOOKS[target]
    created_id = uuid.uuid4()
    seen: list[Any] = []

    async def hook(session: Any, tenant: Any, request: Any) -> tuple[uuid.UUID, str]:
        seen.append(request)
        return created_id, "PR-0042"

    fake = types.ModuleType(module_name)
    setattr(fake, function_name, hook)
    monkeypatch.setattr(service.importlib, "import_module", _fake_import({module_name: fake}))

    request = _request(target)
    result = await service._invoke_conversion_hook(
        _Untouchable(),  # type: ignore[arg-type]
        _tenant(),
        target,
        request,
    )
    assert result == (created_id, "PR-0042")
    assert seen == [request]


@pytest.mark.parametrize("target", list(ConversionTarget))
def test_every_deployed_module_either_exposes_its_hook_or_documents_why_not(
    target: ConversionTarget,
) -> None:
    """The gap this test exists for: a hook named here that the module never defined."""
    module_name, function_name = service.CONVERSION_HOOKS[target]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return
    if hasattr(module, function_name):
        assert callable(getattr(module, function_name))
    else:
        assert target in service.MANUAL_CONVERSION_REASONS, (
            f"{module_name} is deployed but defines no {function_name}, and no manual "
            "conversion reason is recorded for it"
        )


def test_the_purchase_hook_exists_in_procurement() -> None:
    """The default target for every PURCHASE suggestion must actually create something."""
    module_name, function_name = service.CONVERSION_HOOKS[ConversionTarget.PURCHASE_REQUISITION]
    assert callable(getattr(importlib.import_module(module_name), function_name))


# -----------------------------------------------------------------------------
# One name per record
# -----------------------------------------------------------------------------


class _NoRowSession:
    async def execute(self, statement: Any) -> Any:
        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()


@pytest.mark.parametrize(
    ("repository", "entity"),
    [(MrpRunRepository, MRP_RUN_ENTITY), (MrpSuggestionRepository, MRP_SUGGESTION_ENTITY)],
)
async def test_get_or_404_and_the_lock_path_use_the_same_human_name(
    repository: Any, entity: str
) -> None:
    """'MrpRun not found' from one route and 'MRP run not found' from another was the defect."""
    missing = uuid.uuid4()
    with pytest.raises(NotFoundError) as excinfo:
        await repository(_NoRowSession(), _tenant()).get_or_404(missing)
    assert excinfo.value.message == f"{entity} not found: {missing}"
    assert "Mrp" not in excinfo.value.message
    assert entity in (service.RUN_LIFECYCLE.entity, service.SUGGESTION_LIFECYCLE.entity)
