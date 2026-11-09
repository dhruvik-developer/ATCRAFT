"""Sales order write routes over HTTP: create, edit, confirm, approve and close.

``tests/unit/test_route_authorization.py`` and ``tests/api/test_route_contract_http.py``
already prove every route refuses an anonymous or unentitled caller and answers an
unknown id with 404. The pricing, credit and reservation rules are pinned at
service level in ``tests/integration/test_sales_service.py``. This file walks the
five handlers no test had executed through HTTP: the status and envelope each
answers with, the row and the reservation it leaves behind, the audit entry
written in the same transaction, the 422 a payload or the order's state earns,
and the 404 another company's order answers with.

The desk is one finished good (sale price 100, standard cost 60, GST 18%) with
100 pieces on hand in a finished-goods store, and a customer with no address -
so every order is inter-state and its tax is 18% either way. Orders are raised
through ``sales.create_order`` by the ``api_client`` principal's own user, which
is why approving one needs a second person. Celery's ``send_task`` is stubbed:
issuing an invoice queues its PDF. Fixtures hand back plain ids, never ORM rows -
an expected failure rolls its savepoint back and expires what was loaded before it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext
from src.core.permissions import Permission
from src.modules.allocation.models import ReservationStatus, StockReservation
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.dispatch import service as dispatch_service
from src.modules.dispatch.schemas import DispatchCreate, DispatchRequest
from src.modules.finance import sales_invoice_service
from src.modules.finance.sales_invoice_schemas import SalesInvoiceCreate
from src.modules.inventory import service as inventory
from src.modules.inventory.models import TransactionType
from src.modules.products.models import ItemGroup
from src.modules.sales import service as sales
from src.modules.sales.models import SalesOrder, SalesOrderLine, SalesOrderStatus
from src.modules.sales.schemas import SalesOrderCreate, SalesOrderLineIn
from src.modules.vendors.models import Transporter
from src.modules.warehouses.models import WarehouseType
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.api.vendor_portal_support import sign_in

pytestmark = [pytest.mark.api, pytest.mark.integration]

WEEK = timedelta(days=7)


# -----------------------------------------------------------------------------
# Envelope and row helpers
# -----------------------------------------------------------------------------


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def refused(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["code"] == code, body["error"]
    return body["error"]


def field_errors(response: Response) -> dict[str, list[str]]:
    return refused(response, 422, "VALIDATION_ERROR")["details"]["fields"]


async def audit_trail(
    session: AsyncSession, entity_id: uuid.UUID, action: AuditAction | None = None
) -> list[AuditLog]:
    # (created_at, id): every row written in one transaction shares now().
    stmt = select(AuditLog).where(
        AuditLog.entity_type == sales.AUDIT_SALES_ORDER, AuditLog.entity_id == entity_id
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.scalars(stmt.order_by(AuditLog.created_at, AuditLog.id))).all())


def moves(trail: list[AuditLog]) -> list[tuple[str, str]]:
    return [
        ((row.old_values or {}).get("status"), (row.new_values or {}).get("status"))
        for row in trail
        if row.action == AuditAction.STATUS_CHANGE
    ]


async def order_status(session: AsyncSession, order_id: uuid.UUID) -> str:
    return str(await session.scalar(select(SalesOrder.status).where(SalesOrder.id == order_id)))


def colleague_principal(
    base: Principal, user_id: uuid.UUID, company_id: uuid.UUID | None = None
) -> Principal:
    """A second member of staff holding every right: segregation of duties needs one."""
    return Principal(
        user_id=user_id,
        company_id=company_id or base.company_id,
        email=f"colleague-{user_id.hex[:6]}@test.local",
        full_name="Sales Manager",
        is_superuser=False,
        permissions=frozenset(str(p) for p in Permission),
        role_codes=("SALES_MANAGER",),
    )


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Every task name handed to Celery. Nothing reaches a broker."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


async def sales_desk(
    session: AsyncSession, tenant: TenantContext, company: Any, unique_code
) -> SimpleNamespace:
    """A sellable item with 100 on hand, a store and a customer, in one company."""
    company.state_code, company.state = "27", "Maharashtra"
    await session.flush()
    uom = await seed_support.seed_uom(session, company_id=tenant.company_id)
    item = await seed_support.seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("FG"),
        base_uom_id=uom.id,
        item_group=ItemGroup.FINISHED_GOOD,
        can_sell=True,
        sale_price=Decimal("100"),
        standard_cost=Decimal("60"),
        gst_rate=Decimal("18"),
        hsn_code="8302",
    )
    warehouse = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=unique_code("FGW"),
        warehouse_type=WarehouseType.FINISHED_GOODS,
    )
    customer = await seed_support.seed_customer(
        session, company_id=tenant.company_id, code=unique_code("CU"), credit_days=30
    )
    await inventory.post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("100"),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="opening",
        reference_id=None,
        reference_code="OPENING",
        reason="Sales routes fixture stock",
        unit_cost=Decimal("60"),
    )
    return SimpleNamespace(
        item_id=item.id, uom_id=uom.id, warehouse_id=warehouse.id, customer_id=customer.id
    )


@pytest.fixture
async def desk(session, tenant, company, unique_code) -> SimpleNamespace:
    return await sales_desk(session, tenant, company, unique_code)


@pytest.fixture
async def foreign(session, other_company) -> TenantContext:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


@pytest.fixture
async def colleague(session, tenant, principal) -> Principal:
    user = await seed_support.seed_user(session, company_id=tenant.company_id)
    return colleague_principal(principal, user.id)


def order_body(desk: SimpleNamespace, quantity: str = "10", **line: Any) -> dict[str, Any]:
    return {
        "customer_id": str(desk.customer_id),
        "warehouse_id": str(desk.warehouse_id),
        "required_date": (today() + WEEK).isoformat(),
        "customer_po_number": "PO-CUST-77",
        "lines": [{"line_number": 1, "item_id": str(desk.item_id), "quantity": quantity, **line}],
    }


async def place(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    desk: SimpleNamespace,
    *,
    quantity: str = "10",
    unit_price: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A DRAFT order of one line through the service. Returns (order id, line id)."""
    order, lines = await sales.create_order(
        session,
        tenant,
        principal,
        SalesOrderCreate(
            customer_id=desk.customer_id,
            warehouse_id=desk.warehouse_id,
            required_date=today() + WEEK,
            lines=[
                SalesOrderLineIn(
                    line_number=1,
                    item_id=desk.item_id,
                    quantity=Decimal(quantity),
                    unit_price=None if unit_price is None else Decimal(unit_price),
                )
            ],
        ),
    )
    return order.id, lines[0].id


async def confirmed(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    desk: SimpleNamespace,
    *,
    quantity: str = "10",
) -> tuple[uuid.UUID, uuid.UUID]:
    order_id, line_id = await place(session, tenant, principal, desk, quantity=quantity)
    await sales.confirm_order(session, tenant, principal, order_id, reason="Customer committed")
    return order_id, line_id


async def invoiced(
    session: AsyncSession, tenant: TenantContext, principal: Principal, desk: SimpleNamespace
) -> uuid.UUID:
    """Confirmed, shipped in full and invoiced - the one status ``close`` accepts."""
    order_id, line_id = await confirmed(session, tenant, principal, desk)
    consignment = await dispatch_service.create_dispatch(
        session,
        tenant,
        DispatchCreate.model_validate(
            {
                "dispatch_type": "SALES",
                "customer_id": str(desk.customer_id),
                "warehouse_id": str(desk.warehouse_id),
                "sales_order_id": str(order_id),
                "dispatch_date": today().isoformat(),
                "lines": [
                    {
                        "item_id": str(desk.item_id),
                        "sales_order_line_id": str(line_id),
                        "quantity": "10",
                    }
                ],
            }
        ),
    )
    await dispatch_service.mark_ready(session, tenant, consignment.id, reason="Loaded and ready")
    await dispatch_service.dispatch_consignment(
        session, tenant, consignment.id, DispatchRequest(reason="Goods leaving")
    )
    await sales_invoice_service.issue_sales_invoice(
        session, tenant, SalesInvoiceCreate(dispatch_id=consignment.id)
    )
    assert await order_status(session, order_id) == SalesOrderStatus.INVOICED
    return order_id


# -----------------------------------------------------------------------------
# POST /sales
# -----------------------------------------------------------------------------


async def test_a_draft_is_priced_from_the_item_reserves_nothing_and_is_audited(
    api_client, session, tenant, desk, decimal_equal
):
    data = ok(await api_client.post("/sales", json=order_body(desk)), 201)

    assert (data["status"], data["source"], data["customer_po_number"]) == (
        "DRAFT",
        "ERP",
        "PO-CUST-77",
    )
    assert data["requires_approval"] is False
    [line] = data["lines"]
    decimal_equal(line["unit_price"], "100")
    decimal_equal(line["allocated_quantity"], "0")
    decimal_equal(data["subtotal"], "1000")
    decimal_equal(data["tax_amount"], "180")
    decimal_equal(data["total_amount"], "1180")

    order_id = uuid.UUID(data["id"])
    stored = await session.get(SalesOrder, order_id)
    assert stored is not None
    assert (stored.code, stored.status, stored.company_id) == (
        data["code"],
        SalesOrderStatus.DRAFT,
        tenant.company_id,
    )
    reservations = await session.scalar(
        select(func.count())
        .select_from(StockReservation)
        .where(StockReservation.demand_id == order_id)
    )
    assert reservations == 0

    [created] = await audit_trail(session, order_id, AuditAction.CREATE)
    assert created.created_by_id == tenant.user_id
    assert created.new_values["line_count"] == 1
    assert created.new_values["below_cost_lines"] == []


async def test_a_below_cost_override_flags_the_draft_and_opens_an_approval(
    api_client, session, desk
):
    data = ok(await api_client.post("/sales", json=order_body(desk, unit_price="50")), 201)

    assert data["requires_approval"] is True
    order_id = uuid.UUID(data["id"])
    [created] = await audit_trail(session, order_id, AuditAction.CREATE)
    assert created.new_values["below_cost_lines"] == [1]
    assert created.new_values["price_overrides"] == [1]
    pending = (
        await session.execute(
            select(ApprovalRequest.requested_action, ApprovalRequest.status).where(
                ApprovalRequest.entity_id == order_id
            )
        )
    ).all()
    assert pending == [(str(Permission.SALES_ORDER_APPROVE), "PENDING")]


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda body: body.update(lines=[]), "lines"),
        (lambda body: body["lines"][0].update(quantity="0"), "lines.0.quantity"),
        (
            lambda body: body["lines"][0].update(discount_percentage="150"),
            "lines.0.discount_percentage",
        ),
        (lambda body: body["lines"][0].update(colour="Rose gold"), "lines.0.colour"),
        (lambda body: body.update(priority=42), "priority"),
        (lambda body: body.pop("warehouse_id"), "warehouse_id"),
    ],
    ids=["no-lines", "zero-quantity", "discount-over-100", "unknown-field", "priority", "no-store"],
)
async def test_a_malformed_order_is_a_field_error_and_writes_nothing(
    api_client, session, tenant, desk, mutate, field
):
    body = order_body(desk)
    mutate(body)

    assert field in field_errors(await api_client.post("/sales", json=body))
    count = await session.scalar(
        select(func.count())
        .select_from(SalesOrder)
        .where(SalesOrder.company_id == tenant.company_id)
    )
    assert count == 0


async def test_a_required_date_before_the_order_date_is_refused(api_client, desk):
    body = order_body(desk)
    body.update(order_date=today().isoformat(), required_date=(today() - WEEK).isoformat())

    assert field_errors(await api_client.post("/sales", json=body))


@pytest.mark.parametrize("party", ["customer_id", "warehouse_id"])
async def test_another_companys_customer_or_store_is_not_found(
    api_client, session, tenant, other_company, desk, unique_code, party
):
    theirs = {
        "customer_id": (await seed_support.seed_customer(session, company_id=other_company.id)).id,
        "warehouse_id": (
            await seed_support.seed_warehouse(
                session, company_id=other_company.id, code=unique_code("WH")
            )
        ).id,
    }
    body = order_body(desk)
    body[party] = str(theirs[party])

    refused(await api_client.post("/sales", json=body), 404, "NOT_FOUND")
    count = await session.scalar(
        select(func.count())
        .select_from(SalesOrder)
        .where(SalesOrder.company_id == tenant.company_id)
    )
    assert count == 0


async def carrier(session: AsyncSession, company_id: uuid.UUID, unique_code) -> uuid.UUID:
    row = Transporter(
        company_id=company_id,
        code=unique_code("T"),
        name="Elsewhere Roadways",
        transporter_type="COURIER",
    )
    session.add(row)
    await session.flush()
    return row.id


async def test_this_companys_transporter_is_kept_on_an_order(
    api_client, session, company, desk, unique_code
):
    ours = await carrier(session, company.id, unique_code)
    body = order_body(desk)
    body["transporter_id"] = str(ours)

    data = ok(await api_client.post("/sales", json=body), 201)

    assert data["transporter_id"] == str(ours)


async def test_another_companys_transporter_is_not_accepted_on_an_order(
    api_client, session, other_company, desk, unique_code
):
    theirs = await carrier(session, other_company.id, unique_code)
    body = order_body(desk)
    body["transporter_id"] = str(theirs)

    refused(await api_client.post("/sales", json=body), 404, "NOT_FOUND")


@pytest.mark.parametrize("stage", ["draft", "confirmed"])
async def test_an_edit_may_change_the_carrier_only_to_one_of_this_companys(
    api_client, session, tenant, principal, company, other_company, desk, unique_code, stage
):
    make = place if stage == "draft" else confirmed
    order_id, _ = await make(session, tenant, principal, desk)
    theirs = await carrier(session, other_company.id, unique_code)
    ours = await carrier(session, company.id, unique_code)

    refused(
        await api_client.patch(f"/sales/{order_id}", json={"transporter_id": str(theirs)}),
        404,
        "NOT_FOUND",
    )
    assert (
        await session.scalar(select(SalesOrder.transporter_id).where(SalesOrder.id == order_id))
        is None
    )

    data = ok(await api_client.patch(f"/sales/{order_id}", json={"transporter_id": str(ours)}))
    assert data["transporter_id"] == str(ours)


# -----------------------------------------------------------------------------
# PATCH /sales/{order_id}
# -----------------------------------------------------------------------------


async def test_a_draft_is_repriced_when_its_lines_are_replaced(
    api_client, session, tenant, principal, desk, decimal_equal
):
    order_id, _ = await place(session, tenant, principal, desk)
    body = {
        "notes": "Customer doubled the order",
        "lines": [{"line_number": 1, "item_id": str(desk.item_id), "quantity": "20"}],
    }

    data = ok(await api_client.patch(f"/sales/{order_id}", json=body))

    assert data["status"] == "DRAFT"
    assert data["notes"] == "Customer doubled the order"
    [line] = data["lines"]
    decimal_equal(line["quantity"], "20")
    decimal_equal(data["total_amount"], "2360")
    stored_lines = (
        await session.scalars(
            select(SalesOrderLine.quantity).where(SalesOrderLine.sales_order_id == order_id)
        )
    ).all()
    assert [Decimal(q) for q in stored_lines] == [Decimal("20")]
    [updated] = await audit_trail(session, order_id, AuditAction.UPDATE)
    assert Decimal(updated.old_values["total_amount"]) == Decimal("1180")
    assert Decimal(updated.new_values["total_amount"]) == Decimal("2360")


async def test_raising_a_confirmed_quantity_moves_the_reservation_with_it(
    api_client, session, tenant, principal, desk, decimal_equal
):
    order_id, _ = await confirmed(session, tenant, principal, desk)
    body = {"lines": [{"line_number": 1, "item_id": str(desk.item_id), "quantity": "15"}]}

    data = ok(await api_client.patch(f"/sales/{order_id}", json=body))

    assert data["status"] == "ALLOCATED"
    decimal_equal(data["lines"][0]["allocated_quantity"], "15")
    held = await session.scalar(
        select(func.sum(StockReservation.remaining_quantity)).where(
            StockReservation.demand_id == order_id,
            StockReservation.status == ReservationStatus.ACTIVE,
        )
    )
    decimal_equal(held, "15")


@pytest.mark.parametrize("edit", ["frozen-store", "added-line"])
async def test_a_confirmed_order_refuses_edits_outside_quantities_and_dates(
    api_client, session, tenant, principal, desk, unique_code, edit
):
    order_id, _ = await confirmed(session, tenant, principal, desk)
    if edit == "frozen-store":
        store = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        body: dict[str, Any] = {"warehouse_id": str(store.id)}
    else:
        body = {
            "lines": [
                {"line_number": 1, "item_id": str(desk.item_id), "quantity": "10"},
                {"line_number": 2, "item_id": str(desk.item_id), "quantity": "5"},
            ]
        }

    error = refused(
        await api_client.patch(f"/sales/{order_id}", json=body), 422, "BUSINESS_RULE_VIOLATION"
    )
    if edit == "frozen-store":
        assert error["details"]["frozen_fields"] == ["warehouse_id"]
    assert await audit_trail(session, order_id, AuditAction.UPDATE) == []


async def test_a_cancelled_order_can_no_longer_be_edited(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await place(session, tenant, principal, desk)
    await sales.cancel_order(session, tenant, order_id, reason="Customer withdrew")

    refused(
        await api_client.patch(f"/sales/{order_id}", json={"notes": "Too late"}),
        422,
        "BUSINESS_RULE_VIOLATION",
    )


async def test_an_edit_outside_the_schema_is_a_field_error(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await place(session, tenant, principal, desk)

    fields = field_errors(
        await api_client.patch(f"/sales/{order_id}", json={"priority": 0, "status": "CLOSED"})
    )

    assert {"priority", "status"} <= set(fields)
    assert await order_status(session, order_id) == SalesOrderStatus.DRAFT


# -----------------------------------------------------------------------------
# POST /sales/{order_id}/confirm
# -----------------------------------------------------------------------------


async def test_confirming_with_stock_on_hand_reserves_every_line(
    api_client, session, tenant, principal, desk, decimal_equal
):
    order_id, _ = await place(session, tenant, principal, desk)

    data = ok(
        await api_client.post(f"/sales/{order_id}/confirm", json={"reason": "Customer committed"})
    )

    assert data["order"]["status"] == "ALLOCATED"
    assert data["allocation"]["fully_allocated"] is True
    assert data["allocation"]["total_shortfall_lines"] == 0
    decimal_equal(data["allocation"]["lines"][0]["reserved_quantity"], "10")
    balance = await inventory.get_balance(session, tenant, desk.item_id, desk.warehouse_id)
    assert balance is not None
    decimal_equal(balance.reserved_quantity, "10")

    trail = await audit_trail(session, order_id)
    assert moves(trail) == [("DRAFT", "CONFIRMED"), ("CONFIRMED", "ALLOCATED")]
    [reserved] = [row for row in trail if row.action == AuditAction.RESERVATION_CHANGE]
    assert reserved.context["reason"] == "Customer committed"
    assert reserved.new_values["short_lines"] == 0


async def test_confirming_short_of_stock_reserves_what_exists_and_reports_the_rest(
    api_client, session, tenant, principal, desk, decimal_equal
):
    order_id, _ = await place(session, tenant, principal, desk, quantity="150")

    data = ok(await api_client.post(f"/sales/{order_id}/confirm", json={}))

    assert data["order"]["status"] == "CONFIRMED"
    assert data["allocation"]["fully_allocated"] is False
    assert data["allocation"]["total_shortfall_lines"] == 1
    [line] = data["allocation"]["lines"]
    decimal_equal(line["reserved_quantity"], "100")
    decimal_equal(line["shortfall_quantity"], "50")
    assert moves(await audit_trail(session, order_id)) == [("DRAFT", "CONFIRMED")]


async def test_a_short_confirmed_order_cannot_be_confirmed_again(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await confirmed(session, tenant, principal, desk, quantity="150")

    refused(
        await api_client.post(f"/sales/{order_id}/confirm", json={}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    assert moves(await audit_trail(session, order_id)) == [("DRAFT", "CONFIRMED")]


async def test_an_allocated_order_cannot_be_confirmed_again(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await confirmed(session, tenant, principal, desk)

    error = refused(
        await api_client.post(f"/sales/{order_id}/confirm", json={}),
        422,
        "INVALID_STATE_TRANSITION",
    )
    assert error["details"]["current_status"] == "ALLOCATED"
    assert moves(await audit_trail(session, order_id)) == [
        ("DRAFT", "CONFIRMED"),
        ("CONFIRMED", "ALLOCATED"),
    ]


async def test_a_below_cost_draft_needs_its_approval_before_confirming(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await place(session, tenant, principal, desk, unit_price="50")

    refused(await api_client.post(f"/sales/{order_id}/confirm", json={}), 422, "APPROVAL_REQUIRED")
    assert await order_status(session, order_id) == SalesOrderStatus.DRAFT
    assert moves(await audit_trail(session, order_id)) == []


@pytest.mark.parametrize("action", ["confirm", "approve", "close"])
async def test_a_lifecycle_reason_must_be_a_sentence(
    api_client, session, tenant, principal, desk, action
):
    order_id, _ = await place(session, tenant, principal, desk, unit_price="50")

    fields = field_errors(
        await api_client.post(f"/sales/{order_id}/{action}", json={"reason": "ok", "by": "me"})
    )

    assert {"reason", "by"} <= set(fields)
    stored = await session.get(SalesOrder, order_id)
    assert stored is not None
    assert (stored.status, stored.approved_at) == (SalesOrderStatus.DRAFT, None)


# -----------------------------------------------------------------------------
# POST /sales/{order_id}/approve
# -----------------------------------------------------------------------------


async def test_a_second_person_approves_a_below_cost_draft_so_it_can_be_confirmed(
    api_client, session, tenant, principal, colleague, desk
):
    order_id, _ = await place(session, tenant, principal, desk, unit_price="50")
    sign_in(colleague)

    data = ok(
        await api_client.post(
            f"/sales/{order_id}/approve", json={"reason": "Clearance price agreed"}
        )
    )

    assert data["approved_by_id"] == str(colleague.user_id)
    assert data["approved_at"] is not None
    assert data["status"] == "DRAFT"
    [approved] = await audit_trail(session, order_id, AuditAction.APPROVE)
    assert approved.created_by_id == colleague.user_id
    assert approved.context["reason"] == "Clearance price agreed"
    decision = (
        await session.execute(
            select(ApprovalRequest.status, ApprovalRequest.decided_by_id).where(
                ApprovalRequest.entity_id == order_id
            )
        )
    ).one()
    assert tuple(decision) == ("APPROVED", colleague.user_id)

    confirmed_now = ok(await api_client.post(f"/sales/{order_id}/confirm", json={}))
    assert confirmed_now["order"]["status"] == "ALLOCATED"


async def test_the_author_of_an_order_cannot_approve_it(
    api_client, session, tenant, principal, desk
):
    order_id, _ = await place(session, tenant, principal, desk, unit_price="50")

    refused(await api_client.post(f"/sales/{order_id}/approve", json={}), 403, "FORBIDDEN")
    stored = await session.get(SalesOrder, order_id)
    assert stored is not None and stored.approved_at is None
    assert await audit_trail(session, order_id, AuditAction.APPROVE) == []


async def test_an_order_is_approved_once_and_only_while_it_is_a_draft(
    api_client, session, tenant, principal, colleague, desk
):
    twice_id, _ = await place(session, tenant, principal, desk, unit_price="50")
    late_id, _ = await confirmed(session, tenant, principal, desk)
    sign_in(colleague)
    ok(await api_client.post(f"/sales/{twice_id}/approve", json={}))

    refused(
        await api_client.post(f"/sales/{twice_id}/approve", json={}), 422, "BUSINESS_RULE_VIOLATION"
    )
    refused(
        await api_client.post(f"/sales/{late_id}/approve", json={}), 422, "BUSINESS_RULE_VIOLATION"
    )
    assert len(await audit_trail(session, twice_id, AuditAction.APPROVE)) == 1


# -----------------------------------------------------------------------------
# POST /sales/{order_id}/close
# -----------------------------------------------------------------------------


async def test_an_invoiced_order_is_closed_with_its_reason_on_record(
    api_client, session, tenant, principal, desk
):
    order_id = await invoiced(session, tenant, principal, desk)

    data = ok(
        await api_client.post(f"/sales/{order_id}/close", json={"reason": "Paid and delivered"})
    )

    assert data["status"] == "CLOSED"
    assert await order_status(session, order_id) == SalesOrderStatus.CLOSED
    closing = [
        row
        for row in await audit_trail(session, order_id, AuditAction.STATUS_CHANGE)
        if (row.new_values or {}).get("status") == "CLOSED"
    ]
    assert [(row.old_values["status"], row.context["reason"]) for row in closing] == [
        ("INVOICED", "Paid and delivered")
    ]


async def test_an_order_that_was_never_invoiced_cannot_be_closed(
    api_client, session, tenant, principal, desk
):
    draft_id, _ = await place(session, tenant, principal, desk)
    allocated_id, _ = await confirmed(session, tenant, principal, desk)

    for order_id in (draft_id, allocated_id):
        refused(
            await api_client.post(f"/sales/{order_id}/close", json={}),
            422,
            "INVALID_STATE_TRANSITION",
        )
    assert await order_status(session, draft_id) == SalesOrderStatus.DRAFT
    assert await order_status(session, allocated_id) == SalesOrderStatus.ALLOCATED


# -----------------------------------------------------------------------------
# Tenant isolation: another company's order
# -----------------------------------------------------------------------------


async def test_another_companys_order_is_not_found_by_any_write(
    api_client, session, other_company, foreign, principal, unique_code
):
    theirs = await sales_desk(session, foreign, other_company, unique_code)
    their_seller = colleague_principal(principal, foreign.user_id, other_company.id)
    their_order_id, _ = await place(session, foreign, their_seller, theirs)
    base = f"/sales/{their_order_id}"

    refused(await api_client.patch(base, json={"notes": "Hijacked"}), 404, "NOT_FOUND")
    for action in ("confirm", "approve", "close"):
        refused(await api_client.post(f"{base}/{action}", json={}), 404, "NOT_FOUND")

    stored = await session.get(SalesOrder, their_order_id)
    assert stored is not None
    assert (stored.status, stored.notes, stored.approved_at) == (SalesOrderStatus.DRAFT, None, None)
