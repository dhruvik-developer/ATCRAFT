"""Job-work invoices over HTTP: the typed create, the filter and billable lines.

The service rules are covered in
``tests/integration/test_job_work_invoice_service.py``. These assert the
boundary: the billable-lines feed answers to either of its two rights and to
neither without one, another company's order answers 404 - never 403, which
would confirm it exists - and the typed create body reaches the service
unchanged, including the ``invoice_type`` filter the job-work "Invoices" tab
lists through.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import Principal
from src.core.permissions import Permission
from src.modules.job_work.models import (
    JobWorkOrder,
    JobWorkOrderLine,
    JobWorkOrderStatus,
    JobWorkProcess,
    JobWorkReceipt,
    JobWorkReceiptLine,
    RateBasis,
)
from src.modules.vendors.models import VendorType
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]


def _holding(base: Principal, *permissions: Permission) -> Principal:
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


def _as(principal: Principal) -> None:
    from src.core.deps import get_current_principal
    from src.main import app

    app.dependency_overrides[get_current_principal] = lambda: principal


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


async def _shop(session: AsyncSession, company_id: uuid.UUID) -> dict[str, str]:
    """An issued order with one receipt line carrying unbilled charges."""
    vendor = await seed_support.seed_vendor(
        session, company_id=company_id, vendor_types=(VendorType.JOB_WORK,)
    )
    uom = await seed_support.seed_uom(session, company_id=company_id)
    item = await seed_support.seed_item(
        session, company_id=company_id, code=_code("LEVER"), base_uom_id=uom.id
    )
    warehouse = await seed_support.seed_warehouse(session, company_id=company_id, code=_code("W"))
    order = JobWorkOrder(
        company_id=company_id,
        code=_code("JW"),
        vendor_id=vendor.id,
        process_type=JobWorkProcess.COATING,
        expected_return_date=date(2026, 5, 20),
        warehouse_id=warehouse.id,
        return_warehouse_id=warehouse.id,
        rate_per_unit=Decimal("18.50"),
        rate_basis=RateBasis.PER_PIECE,
        estimated_charges=Decimal("1073.00"),
        status=JobWorkOrderStatus.ISSUED,
        tax_rate=Decimal("18"),
        sac_code="9988",
    )
    session.add(order)
    await session.flush()
    order_line = JobWorkOrderLine(
        company_id=company_id,
        job_work_order_id=order.id,
        line_number=1,
        item_id=item.id,
        uom_id=uom.id,
        quantity_to_issue=Decimal("58"),
        expected_output_item_id=item.id,
        expected_output_quantity=Decimal("58"),
    )
    receipt = JobWorkReceipt(
        company_id=company_id,
        code=_code("R"),
        receipt_number=1,
        job_work_order_id=order.id,
        vendor_id=vendor.id,
        receipt_date=date(2026, 5, 10),
        total_output_quantity=Decimal("58"),
        total_input_consumed=Decimal("58"),
        total_charges=Decimal("1073.00"),
        reason="Plated levers received back and counted",
    )
    session.add_all([order_line, receipt])
    await session.flush()
    receipt_line = JobWorkReceiptLine(
        company_id=company_id,
        receipt_id=receipt.id,
        job_work_order_line_id=order_line.id,
        line_number=1,
        output_item_id=item.id,
        output_quantity=Decimal("58"),
        input_consumed_quantity=Decimal("58"),
        charges=Decimal("1073.00"),
    )
    session.add(receipt_line)
    await session.flush()
    return {
        "vendor_id": str(vendor.id),
        "order_id": str(order.id),
        "receipt_line_id": str(receipt_line.id),
    }


def _invoice_body(shop: dict[str, str], **overrides: object) -> dict[str, object]:
    return {
        "vendor_id": shop["vendor_id"],
        "vendor_invoice_number": _code("CHR"),
        "invoice_date": "2026-05-12",
        "invoice_type": "JOB_WORK",
        "job_work_order_id": shop["order_id"],
        "lines": [
            {
                "job_work_receipt_line_id": shop["receipt_line_id"],
                "description": "Antique brass plating - lever sets",
                "hsn_code": "9988",
                "quantity": "58",
                "unit_price": "18.50",
                "tax_rate": "18",
            }
        ],
        **overrides,
    }


class TestBillableLinesPermissions:
    async def test_either_right_reaches_the_feed(self, api_client, principal, session) -> None:
        shop = await _shop(session, principal.company_id)

        _as(_holding(principal, Permission.JOB_WORK_VIEW))
        seen = await api_client.get(f"/job-work/{shop['order_id']}/billable-lines")
        assert seen.status_code == 200
        rows = seen.json()["data"]
        assert len(rows) == 1
        assert rows[0]["unbilled_amount"] == "1073.00"
        assert rows[0]["receipt_code"].startswith("R-")

        _as(_holding(principal, Permission.VENDOR_INVOICE_CREATE))
        again = await api_client.get(f"/job-work/{shop['order_id']}/billable-lines")
        assert again.status_code == 200

    async def test_no_right_is_refused_and_another_tenant_404s(
        self, api_client, principal, session
    ) -> None:
        shop = await _shop(session, principal.company_id)

        _as(_holding(principal, Permission.FINANCE_VIEW))
        assert (
            await api_client.get(f"/job-work/{shop['order_id']}/billable-lines")
        ).status_code == 403

        _as(principal)
        assert (await api_client.get(f"/job-work/{uuid.uuid4()}/billable-lines")).status_code == 404


class TestTypedCreateOverHttp:
    async def test_create_submit_match_and_filter(self, api_client, principal, session) -> None:
        shop = await _shop(session, principal.company_id)
        _as(principal)

        created = await api_client.post("/finance/vendor-invoices", json=_invoice_body(shop))
        assert created.status_code == 201
        invoice = created.json()["data"]
        assert invoice["invoice_type"] == "JOB_WORK"
        assert invoice["job_work_order_id"] == shop["order_id"]
        assert invoice["lines"][0]["job_work_receipt_line_id"] == shop["receipt_line_id"]

        submitted = await api_client.post(f"/finance/vendor-invoices/{invoice['id']}/submit")
        assert submitted.status_code == 200
        assert submitted.json()["data"]["match_status"] == "MATCHED"

        matched = await api_client.get(f"/finance/vendor-invoices/{invoice['id']}/match")
        assert matched.status_code == 200
        body = matched.json()["data"]
        assert body["matched"] is True
        assert body["job_work_order_id"] == shop["order_id"]
        assert body["lines"][0]["job_work_receipt_line_id"] == shop["receipt_line_id"]

        filtered = await api_client.get(
            "/finance/vendor-invoices", params={"invoice_type": "JOB_WORK"}
        )
        assert filtered.status_code == 200
        assert invoice["id"] in [row["id"] for row in filtered.json()["data"]]

    async def test_a_purchase_bill_naming_a_job_work_order_is_refused(
        self, api_client, principal, session
    ) -> None:
        shop = await _shop(session, principal.company_id)
        _as(principal)

        refused = await api_client.post(
            "/finance/vendor-invoices",
            json=_invoice_body(shop, invoice_type="PURCHASE", job_work_order_id=shop["order_id"]),
        )
        assert refused.status_code == 422
