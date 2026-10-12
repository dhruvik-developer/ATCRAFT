"""Vendor-portal principals reach only their own purchase orders.

``VENDOR_OWNER`` and ``VENDOR_MANAGER`` hold ``purchase_order.update`` so they
can acknowledge an order and record its shipment. The same permission gates the
buyer's PATCH and submit, and the repository's lock path once applied only the
tenant filter - so a vendor user could rewrite lines, prices and freight on any
draft order in the company, including a competitor's.

Two guards, both pinned here without a database:

* The repository applies the vendor filter at ``_scoped``, the root of every
  statement it builds, so ``get``, ``get_by_code``, ``lock_for_update`` and the
  list path all inherit it. The statements are compiled and inspected.
* The buyer-only routes carry ``forbid_vendor_users`` beside the permission;
  the vendor-side routes (acknowledge, shipment) deliberately do not.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.routing import APIRoute
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext, forbid_vendor_users
from src.modules.procurement.repository import PurchaseOrderRepository
from src.modules.procurement.router import router
from src.modules.procurement.schemas import PurchaseOrderFilters

pytestmark = pytest.mark.unit


class _Result:
    def scalar_one_or_none(self) -> Any:
        return None

    def scalars(self) -> _Result:
        return self

    def unique(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return []


class _Session:
    """Records every statement so the test can read the SQL that would run."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result()


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _tenant(vendor_id: uuid.UUID | None) -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4(), vendor_id=vendor_id)


VENDOR_FILTER = "purchase_orders.vendor_id = "


# -----------------------------------------------------------------------------
# Repository: the filter is at the root of every statement
# -----------------------------------------------------------------------------


async def test_a_vendor_principal_cannot_lock_another_vendors_order() -> None:
    session = _Session()
    repo = PurchaseOrderRepository(session, _tenant(uuid.uuid4()))  # type: ignore[arg-type]

    assert await repo.lock_for_update(uuid.uuid4()) is None

    sql = _sql(session.statements[0])
    assert "FOR UPDATE" in sql
    assert VENDOR_FILTER in sql
    assert "purchase_orders.company_id = " in sql


async def test_a_vendor_principal_cannot_load_another_vendors_order() -> None:
    session = _Session()
    repo = PurchaseOrderRepository(session, _tenant(uuid.uuid4()))  # type: ignore[arg-type]

    await repo.get(uuid.uuid4())
    await repo.get_by_code("PO-0001")
    await repo.get_many([uuid.uuid4(), uuid.uuid4()])

    assert len(session.statements) == 3
    for statement in session.statements:
        assert VENDOR_FILTER in _sql(statement)


async def test_the_vendor_list_is_scoped_even_when_the_filter_names_no_vendor() -> None:
    """The filter's own ``vendor_id`` is a *narrowing*; the scope is not opt-in."""
    session = _Session()
    repo = PurchaseOrderRepository(session, _tenant(uuid.uuid4()))  # type: ignore[arg-type]

    from datetime import date

    stmt = repo._filtered(PurchaseOrderFilters(), today=date(2026, 9, 5))
    sql = _sql(stmt)
    assert sql.count(VENDOR_FILTER) == 1


async def test_a_buyer_still_reaches_every_vendors_order() -> None:
    """An internal user has no vendor; the receipt and MRP paths depend on that."""
    session = _Session()
    repo = PurchaseOrderRepository(session, _tenant(None))  # type: ignore[arg-type]

    await repo.lock_for_update(uuid.uuid4())
    await repo.get(uuid.uuid4())

    for statement in session.statements:
        sql = _sql(statement)
        assert VENDOR_FILTER not in sql
        assert "purchase_orders.company_id = " in sql


# -----------------------------------------------------------------------------
# Router: buyer-only routes refuse vendor accounts outright
# -----------------------------------------------------------------------------


def _route(method: str, path: str) -> APIRoute:
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"{method} {path} is not a procurement route")


def _dependency_callables(route: APIRoute) -> list[Any]:
    return [dependency.dependency for dependency in route.dependencies]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/procurement/purchase-orders/{order_id}"),
        ("POST", "/procurement/purchase-orders/{order_id}/submit"),
        # WP C1a: a vendor accepts and ships through /vendor-portal. These two
        # stay for a buyer recording what the vendor phoned in, and are closed
        # to vendor accounts (the route allow-list refuses them first).
        ("POST", "/procurement/purchase-orders/{order_id}/acknowledge"),
        ("POST", "/procurement/purchase-orders/{order_id}/shipment"),
        # The buyer-side consignment companions (WP C1a).
        ("GET", "/procurement/inbound-tracking"),
        ("GET", "/procurement/purchase-orders/{order_id}/shipments"),
        ("POST", "/procurement/purchase-orders/{order_id}/shipments"),
        ("PATCH", "/procurement/purchase-orders/{order_id}/shipments/{shipment_id}"),
        ("POST", "/procurement/purchase-orders/{order_id}/shipments/{shipment_id}/delivered"),
        ("POST", "/procurement/purchase-orders/{order_id}/documents"),
        ("GET", "/procurement/purchase-orders/{order_id}/documents"),
        ("POST", "/procurement/purchase-orders/{order_id}/accept-vendor-date"),
        ("POST", "/procurement/purchase-orders/{order_id}/pdf"),
    ],
)
def test_buyer_side_order_routes_refuse_vendor_accounts(method: str, path: str) -> None:
    assert forbid_vendor_users in _dependency_callables(_route(method, path))


# -----------------------------------------------------------------------------
# What a vendor sees of its own orders: sent ones, in its categories
# -----------------------------------------------------------------------------


def _scoped_sql(tenant: TenantContext) -> str:
    repo = PurchaseOrderRepository(_Session(), tenant)  # type: ignore[arg-type]
    return _sql(repo._scoped())


def test_a_vendor_sees_only_orders_it_has_been_sent() -> None:
    """A draft is the buyer's working state, with prices nobody has approved."""
    sql = _scoped_sql(_tenant(uuid.uuid4()))
    assert "purchase_orders.status IN" in sql
    # CANCELLED only once it had been sent.
    assert "purchase_orders.sent_at IS NOT NULL" in sql


def test_the_visible_statuses_are_sent_and_everything_after_it() -> None:
    from src.modules.procurement.models import VENDOR_VISIBLE_PO_STATUSES, PurchaseOrderStatus

    assert set(VENDOR_VISIBLE_PO_STATUSES) == {
        PurchaseOrderStatus.SENT,
        PurchaseOrderStatus.VENDOR_DECLINED,
        PurchaseOrderStatus.ACKNOWLEDGED,
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.QC_PENDING,
        PurchaseOrderStatus.QC_COMPLETED,
        PurchaseOrderStatus.GRN_COMPLETED,
        PurchaseOrderStatus.INVOICED,
        PurchaseOrderStatus.PAID,
        PurchaseOrderStatus.CLOSED,
    }
    hidden = set(PurchaseOrderStatus) - set(VENDOR_VISIBLE_PO_STATUSES)
    assert hidden == {
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.PENDING_APPROVAL,
        PurchaseOrderStatus.APPROVED,
        PurchaseOrderStatus.REJECTED,
        PurchaseOrderStatus.CANCELLED,
    }


def test_a_category_scoped_vendor_user_sees_its_subtrees_or_what_is_assigned_to_it() -> None:
    lock_body = uuid.uuid4()
    tenant = TenantContext(
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        allowed_category_ids=(lock_body,),
    )
    sql = _scoped_sql(tenant)
    assert "item_categories.path LIKE" in sql
    assert "purchase_orders.assigned_vendor_user_id = " in sql
    compiled = (
        PurchaseOrderRepository(_Session(), tenant)
        ._scoped()
        .compile(  # type: ignore[arg-type]
            dialect=postgresql.dialect()
        )
    )
    assert f"%/{lock_body}/%" in compiled.params.values()


def test_an_unrestricted_vendor_user_has_no_category_clause() -> None:
    """Empty means every category of the vendor - the documented contract."""
    where = _scoped_sql(_tenant(uuid.uuid4())).split("WHERE", 1)[1]
    assert "item_categories" not in where
    assert "assigned_vendor_user_id" not in where


def test_an_internal_user_has_neither_the_status_nor_the_category_clause() -> None:
    where = _scoped_sql(_tenant(None)).split("WHERE", 1)[1]
    assert "purchase_orders.status IN" not in where
    assert "item_categories" not in where


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/procurement/purchase-orders/check-duplicates"),
        ("POST", "/procurement/purchase-requisitions/conversion-preview"),
        ("POST", "/procurement/purchase-requisitions/convert-to-orders"),
        ("POST", "/procurement/purchase-requisitions/{requisition_id}/conversion-preview"),
        ("POST", "/procurement/purchase-requisitions/{requisition_id}/convert-to-orders"),
        ("GET", "/procurement/purchase-orders/{order_id}/vendor-assignees"),
    ],
)
def test_the_buyer_side_conversion_and_lookup_routes_refuse_vendor_accounts(
    method: str, path: str
) -> None:
    """``check-duplicates`` answered with other vendors' open orders (T2)."""
    assert forbid_vendor_users in _dependency_callables(_route(method, path))
