"""Production guards that need no database.

* A draft's deletion carries a reason into the audit row, like every other
  module's delete - the route refuses one without it, and the service will not
  take one without it either.
* An order raised from a planning suggestion is received into a warehouse the
  run was scoped to, chosen deterministically when the run named several.
* Forcing a partial issue is refused with the right it needs spelled out, so a
  client can say which permission to ask for - the same shape the route gate
  and the direction check in ``post_material_issue`` already produce.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.core.config import settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.errors import AuthorizationError, ErrorCode
from src.core.exception_handlers import register_exception_handlers
from src.core.permissions import Permission
from src.modules.production import service
from src.modules.production.router import router
from src.modules.production.service import (
    assert_may_force_partial_issue,
    mrp_receiving_warehouse,
)

pytestmark = pytest.mark.unit

PREFIX = settings.api_v1_prefix
ORDER_CODE = "PO/24/0042"


def _principal(*permissions: Permission, superuser: bool = False) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="planner@test.local",
        full_name="Planner",
        is_superuser=superuser,
        permissions=frozenset(str(p) for p in permissions),
    )


# -----------------------------------------------------------------------------
# Deleting a draft carries its reason
# -----------------------------------------------------------------------------


async def _no_database() -> AsyncIterator[None]:
    yield None


def _superuser() -> Principal:
    return _principal(superuser=True)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # A private application holding only the production router and the real
    # exception handlers - see tests/unit/test_inventory_query_binding.py for
    # why the process-global router is not used.
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(router, prefix=PREFIX)
    application.dependency_overrides[get_db] = _no_database
    application.dependency_overrides[get_current_principal] = _superuser
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


class TestDeleteDraftReason:
    async def test_the_route_refuses_a_delete_without_a_reason(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _must_not_run(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the service must not be reached without a reason")

        monkeypatch.setattr(service, "delete_draft_order", _must_not_run)

        response = await client.delete(f"{PREFIX}/production/{uuid.uuid4()}")

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "query.reason" in body["error"]["details"]["fields"]

    async def test_a_too_short_reason_is_a_422_too(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _must_not_run(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("the service must not be reached with an empty reason")

        monkeypatch.setattr(service, "delete_draft_order", _must_not_run)

        response = await client.delete(
            f"{PREFIX}/production/{uuid.uuid4()}", params={"reason": "no"}
        )

        assert response.status_code == 422, response.text
        assert "query.reason" in response.json()["error"]["details"]["fields"]

    async def test_the_reason_reaches_the_service(
        self, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[uuid.UUID, str]] = []

        async def _recorder(session: Any, tenant: Any, order_id: uuid.UUID, *, reason: str) -> None:
            seen.append((order_id, reason))

        monkeypatch.setattr(service, "delete_draft_order", _recorder)
        order_id = uuid.uuid4()

        response = await client.delete(
            f"{PREFIX}/production/{order_id}", params={"reason": "Raised twice by mistake"}
        )

        assert response.status_code == 200, response.text
        assert seen == [(order_id, "Raised twice by mistake")]

    def test_the_service_will_not_delete_without_a_reason(self) -> None:
        """Keyword-only and without a default, as the other modules' deletes are."""
        parameter = inspect.signature(service.delete_draft_order).parameters["reason"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


# -----------------------------------------------------------------------------
# Where an MRP conversion receives its output
# -----------------------------------------------------------------------------


class TestMrpReceivingWarehouse:
    main = uuid.uuid4()
    spare = uuid.uuid4()
    second = uuid.uuid4()

    def test_a_company_wide_run_defers_to_the_item_master(self) -> None:
        assert mrp_receiving_warehouse(scope=[], default_warehouse_id=self.main) == self.main

    def test_a_company_wide_run_with_no_default_has_nowhere_to_receive(self) -> None:
        assert mrp_receiving_warehouse(scope=[], default_warehouse_id=None) is None

    def test_a_single_warehouse_run_beats_the_item_master(self) -> None:
        """Pinned by the integration suite too: the run's scope is where the plan was made."""
        assert (
            mrp_receiving_warehouse(scope=[self.spare], default_warehouse_id=self.main)
            == self.spare
        )

    def test_a_multi_warehouse_run_prefers_the_default_inside_its_scope(self) -> None:
        # Listed second, still chosen: the answer does not depend on tick order.
        assert (
            mrp_receiving_warehouse(scope=[self.spare, self.main], default_warehouse_id=self.main)
            == self.main
        )

    def test_a_multi_warehouse_run_never_receives_outside_its_scope(self) -> None:
        assert (
            mrp_receiving_warehouse(scope=[self.spare, self.second], default_warehouse_id=self.main)
            == self.spare
        )

    def test_a_multi_warehouse_run_with_no_default_takes_the_first_named(self) -> None:
        assert (
            mrp_receiving_warehouse(scope=[self.second, self.spare], default_warehouse_id=None)
            == self.second
        )


# -----------------------------------------------------------------------------
# Forcing a partial issue names the right it needs
# -----------------------------------------------------------------------------


class TestForcedPartialIssue:
    def test_the_picking_right_alone_is_refused_with_the_right_named(self) -> None:
        with pytest.raises(AuthorizationError) as exc:
            assert_may_force_partial_issue(
                _principal(Permission.MATERIAL_ISSUE_CREATE), order_code=ORDER_CODE
            )

        assert exc.value.code is ErrorCode.PERMISSION_DENIED
        assert exc.value.details["required_permission"] == str(Permission.PRODUCTION_ORDER_APPROVE)
        # The spelling an administrator grants, and the order it is about.
        assert str(Permission.PRODUCTION_ORDER_APPROVE) in exc.value.message
        assert ORDER_CODE in exc.value.message

    def test_the_approval_right_passes(self) -> None:
        assert_may_force_partial_issue(
            _principal(Permission.MATERIAL_ISSUE_CREATE, Permission.PRODUCTION_ORDER_APPROVE),
            order_code=ORDER_CODE,
        )

    def test_a_superuser_passes(self) -> None:
        assert_may_force_partial_issue(_principal(superuser=True), order_code=ORDER_CODE)
