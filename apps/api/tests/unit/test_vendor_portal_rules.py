"""Structural scope and inbound status rules."""

from datetime import date
from decimal import Decimal

import pytest

from src.core.deps import vendor_route_allowed
from src.core.permissions import ROLE_PRESETS, Permission
from src.modules.procurement.inbound_repository import inbound_tracking_status, po_is_late
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.procurement.service_rules import PURCHASE_ORDER_LIFECYCLE
from src.workers.tasks.reports import indian_money, render_purchase_order_pdf

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/vendor-portal/me",
        "/api/v1/notifications",
        "/api/v1/notifications/push/config",
        "/api/v1/auth/me",
        "/api/v1/auth/sessions/1",
        "/api/v1/auth/mfa/setup",
    ],
)
def test_vendor_allowed(path):
    assert vendor_route_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/notifications-evil",
        "/api/v1/vendor-portal-evil",
        "/api/v1/auth/login",
        "/api/v1/procurement/purchase-orders",
        "/api/v1/documents",
        "/api/v1/users",
    ],
)
def test_vendor_denied(path):
    assert not vendor_route_allowed(path)


def test_vendor_roles_only_hold_portal_and_notifications():
    for code, spec in ROLE_PRESETS.items():
        if code.startswith("VENDOR_"):
            assert all(
                str(p).startswith("vendor_portal.") or p == Permission.NOTIFICATION_VIEW
                for p in spec["permissions"]
            )
    assert Permission.VENDOR_PORTAL_ACCESS not in ROLE_PRESETS["AUDITOR"]["permissions"]


def test_declined_machine_resend_cancel():
    PURCHASE_ORDER_LIFECYCLE.validate()
    for source, target in [
        (PurchaseOrderStatus.SENT, PurchaseOrderStatus.VENDOR_DECLINED),
        (PurchaseOrderStatus.VENDOR_DECLINED, PurchaseOrderStatus.SENT),
        (PurchaseOrderStatus.VENDOR_DECLINED, PurchaseOrderStatus.CANCELLED),
    ]:
        PURCHASE_ORDER_LIFECYCLE.assert_transition(source, target)


@pytest.mark.parametrize(
    "inputs,expected",
    [
        ({}, "NOT_DISPATCHED"),
        ({"ready": True}, "READY_FOR_DISPATCH"),
        ({"shipment_exists": True, "shipped_today": True}, "DISPATCHED"),
        ({"shipment_exists": True}, "IN_TRANSIT"),
        ({"delivered": True}, "DELIVERED"),
        ({"receipt_exists": True}, "RECEIVED"),
        ({"receipt_exists": True, "qc_pending": True}, "QC_PENDING"),
        ({"receipt_exists": True, "fully_received": True}, "GRN_COMPLETED"),
    ],
)
def test_tracking(inputs, expected):
    args = {
        "fully_received": False,
        "receipt_exists": False,
        "qc_pending": False,
        "shipment_exists": False,
        "delivered": False,
        "shipped_today": False,
        "ready": False,
    }
    args.update(inputs)
    assert inbound_tracking_status(**args) == expected


def test_lateness_and_pdf():
    assert po_is_late(
        status="ACKNOWLEDGED",
        open_quantity=Decimal("1"),
        expected_date=date(2026, 1, 1),
        as_of=date(2026, 1, 2),
    )
    assert not po_is_late(
        status="VENDOR_DECLINED",
        open_quantity=Decimal("1"),
        expected_date=date(2026, 1, 1),
        as_of=date(2026, 1, 2),
    )
    assert indian_money("12345678.9") == "1,23,45,678.90"
    assert render_purchase_order_pdf(
        {"code": "PO-1", "company": {"name": "A & B"}, "lines": []}
    ).startswith(b"%PDF")
