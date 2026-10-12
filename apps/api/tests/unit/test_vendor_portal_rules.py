"""Vendor-portal rules that need no database.

The route allow-list, the vendor role presets, the purchase-order machine's
VENDOR_DECLINED edges, the one ordered tracking rule (in both its Python and
its SQL form), the vendor-app tab buckets, the shared "PO late" predicate and
the PO PDF renderer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import product
from types import SimpleNamespace

import pytest
from sqlalchemy import literal_column
from sqlalchemy.dialects import postgresql

from src.core.deps import vendor_route_allowed
from src.core.permissions import ROLE_PRESETS, VIEW_ONLY, Permission
from src.modules.procurement.inbound_repository import (
    TAB_TRACKING_STATUSES,
    inbound_tracking_status,
    order_tab,
    po_is_late,
)
from src.modules.procurement.inbound_schemas import InboundShipmentCreate, InboundTrackingStatus
from src.modules.procurement.inbound_service import shipment_message
from src.modules.procurement.models import PurchaseOrderStatus
from src.modules.procurement.service_rules import PURCHASE_ORDER_LIFECYCLE
from src.workers.tasks.reports import indian_money, plain_quantity, render_purchase_order_pdf

pytestmark = pytest.mark.unit

S = InboundTrackingStatus
FLAGS = (
    "fully_received",
    "any_receipt",
    "receipt_since_dispatch",
    "qc_pending",
    "shipment_exists",
    "delivered",
    "shipped_today",
    "ready",
)


# -----------------------------------------------------------------------------
# The route allow-list
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/vendor-portal",
        "/api/v1/vendor-portal/me",
        "/api/v1/vendor-portal/purchase-orders/123/accept",
        "/api/v1/notifications",
        "/api/v1/notifications/",
        "/api/v1/notifications/push/config",
        "/api/v1/auth/me",
        "/api/v1/auth/logout",
        "/api/v1/auth/logout-all",
        "/api/v1/auth/change-password",
        "/api/v1/auth/sessions",
        "/api/v1/auth/sessions/1",
        "/api/v1/auth/mfa/setup",
    ],
)
def test_the_vendor_surface_is_reachable(path: str) -> None:
    assert vendor_route_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        # Segment boundaries: a bare prefix match would let these through.
        "/api/v1/notifications-evil",
        "/api/v1/vendor-portal-evil",
        "/api/v1/auth/mex",
        # Everything else - including the staff modules vendor roles once reached.
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/procurement/purchase-orders",
        "/api/v1/procurement/inbound-tracking",
        "/api/v1/dispatch",
        "/api/v1/finance/vendor-invoices",
        "/api/v1/finance/ledger/customer/1",
        "/api/v1/documents",
        "/api/v1/job-work",
        "/api/v1/quality/inspections",
        "/api/v1/reports/jobs/1",
        "/api/v1/users",
        "/api/v1/vendors/options",
    ],
)
def test_everything_else_is_refused(path: str) -> None:
    assert not vendor_route_allowed(path)


# -----------------------------------------------------------------------------
# Vendor roles hold portal rights only
# -----------------------------------------------------------------------------


def test_vendor_roles_only_hold_portal_and_notification_rights() -> None:
    vendor_roles = [code for code, spec in ROLE_PRESETS.items() if spec.get("is_vendor_role")]
    assert len(vendor_roles) == 5
    for code in vendor_roles:
        permissions = ROLE_PRESETS[code]["permissions"]
        assert Permission.VENDOR_PORTAL_ACCESS in permissions, code  # type: ignore[operator]
        assert all(
            str(p).startswith("vendor_portal.") or p == Permission.NOTIFICATION_VIEW
            for p in permissions  # type: ignore[attr-defined]
        ), code


def test_no_portal_right_is_a_view_right() -> None:
    """A ``.view`` name would fold the rights into AUDITOR's VIEW_ONLY bundle."""
    portal = {p for p in Permission if str(p).startswith("vendor_portal.")}
    assert len(portal) == 5
    assert not portal & VIEW_ONLY
    assert not portal & ROLE_PRESETS["AUDITOR"]["permissions"]  # type: ignore[operator]


# -----------------------------------------------------------------------------
# The purchase-order machine
# -----------------------------------------------------------------------------


def test_the_machine_with_vendor_declined_is_still_valid() -> None:
    PURCHASE_ORDER_LIFECYCLE.validate()


@pytest.mark.parametrize(
    ("source", "target", "permission"),
    [
        (
            PurchaseOrderStatus.SENT,
            PurchaseOrderStatus.VENDOR_DECLINED,
            Permission.VENDOR_PORTAL_RESPOND,
        ),
        (
            PurchaseOrderStatus.VENDOR_DECLINED,
            PurchaseOrderStatus.SENT,
            Permission.PURCHASE_ORDER_SEND,
        ),
        (
            PurchaseOrderStatus.VENDOR_DECLINED,
            PurchaseOrderStatus.CANCELLED,
            Permission.PURCHASE_ORDER_CANCEL,
        ),
    ],
)
def test_a_declined_order_is_resent_or_cancelled(
    source: PurchaseOrderStatus, target: PurchaseOrderStatus, permission: Permission
) -> None:
    PURCHASE_ORDER_LIFECYCLE.assert_transition(source, target)
    assert PURCHASE_ORDER_LIFECYCLE.required_permission(source, target) == permission


@pytest.mark.parametrize(
    "target",
    [
        PurchaseOrderStatus.ACKNOWLEDGED,
        PurchaseOrderStatus.PARTIALLY_RECEIVED,
        PurchaseOrderStatus.RECEIVED,
        PurchaseOrderStatus.CLOSED,
    ],
)
def test_a_declined_order_cannot_be_accepted_or_received(target: PurchaseOrderStatus) -> None:
    assert not PURCHASE_ORDER_LIFECYCLE.can_transition(PurchaseOrderStatus.VENDOR_DECLINED, target)


def test_only_a_sent_order_can_be_declined() -> None:
    sources = {
        transition.source
        for transition in PURCHASE_ORDER_LIFECYCLE.transitions
        if transition.target is PurchaseOrderStatus.VENDOR_DECLINED
    }
    assert sources == {PurchaseOrderStatus.SENT}
    assert not PURCHASE_ORDER_LIFECYCLE.is_terminal(PurchaseOrderStatus.VENDOR_DECLINED)


# -----------------------------------------------------------------------------
# The tracking rule
# -----------------------------------------------------------------------------


def _status(**flags: bool) -> InboundTrackingStatus:
    result = inbound_tracking_status(**{name: flags.get(name, False) for name in FLAGS})
    assert isinstance(result, InboundTrackingStatus)
    return result


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, S.NOT_DISPATCHED),
        ({"ready": True}, S.READY_FOR_DISPATCH),
        ({"ready": True, "shipment_exists": True, "shipped_today": True}, S.DISPATCHED),
        ({"shipment_exists": True}, S.IN_TRANSIT),
        ({"shipment_exists": True, "delivered": True}, S.DELIVERED),
        (
            {"shipment_exists": True, "any_receipt": True, "receipt_since_dispatch": True},
            S.RECEIVED,
        ),
        (
            {"any_receipt": True, "receipt_since_dispatch": True, "qc_pending": True},
            S.QC_PENDING,
        ),
        (
            {"any_receipt": True, "receipt_since_dispatch": True, "fully_received": True},
            S.GRN_COMPLETED,
        ),
    ],
)
def test_one_consignment_walks_the_proposal_vocabulary(
    flags: dict[str, bool], expected: InboundTrackingStatus
) -> None:
    assert _status(**flags) is expected


def test_an_earlier_receipt_does_not_hide_the_consignment_now_on_the_road() -> None:
    """Shipment 1 arrived and was received; shipment 2 has left since."""
    on_the_road = {"any_receipt": True, "shipment_exists": True}
    assert _status(**on_the_road) is S.IN_TRANSIT
    assert _status(**on_the_road, shipped_today=True) is S.DISPATCHED
    assert _status(**on_the_road, delivered=True) is S.DELIVERED
    # The earlier receipt's open inspection is a flag on the row, not the status.
    assert _status(**on_the_road, qc_pending=True) is S.IN_TRANSIT


def test_a_fully_received_order_is_completed_whatever_its_consignments_say() -> None:
    late_entry = {"any_receipt": True, "fully_received": True, "shipment_exists": True}
    assert _status(**late_entry) is S.GRN_COMPLETED
    assert _status(**late_entry, qc_pending=True) is S.QC_PENDING


def test_the_rule_is_total() -> None:
    """Every combination of inputs lands on exactly one member of the vocabulary."""
    for values in product((False, True), repeat=len(FLAGS)):
        assert inbound_tracking_status(**dict(zip(FLAGS, values, strict=True))) in set(S)


def test_the_sql_case_lists_the_same_rules_in_the_same_order() -> None:
    """The CASE the board and the dashboard run is rendered from the one rule table."""
    case = inbound_tracking_status(
        **{name: literal_column(name).is_(True) for name in FLAGS},
        sql=True,
    )
    sql = str(case.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    order = [
        S.QC_PENDING,
        S.GRN_COMPLETED,
        S.RECEIVED,
        S.DELIVERED,
        S.DISPATCHED,
        S.IN_TRANSIT,
        S.READY_FOR_DISPATCH,
    ]
    positions = [sql.index(f"THEN '{status.value}'") for status in order]
    assert positions == sorted(positions)
    assert f"ELSE '{S.NOT_DISPATCHED.value}'" in sql


# -----------------------------------------------------------------------------
# Vendor-app tabs and dashboard tiles
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "tracking", "tab"),
    [
        (PurchaseOrderStatus.SENT, S.NOT_DISPATCHED, "to_accept"),
        (PurchaseOrderStatus.ACKNOWLEDGED, S.NOT_DISPATCHED, "accepted"),
        (PurchaseOrderStatus.ACKNOWLEDGED, S.READY_FOR_DISPATCH, "ready"),
        (PurchaseOrderStatus.ACKNOWLEDGED, S.DISPATCHED, "dispatched"),
        (PurchaseOrderStatus.ACKNOWLEDGED, S.IN_TRANSIT, "dispatched"),
        (PurchaseOrderStatus.ACKNOWLEDGED, S.DELIVERED, "delivered"),
        (PurchaseOrderStatus.PARTIALLY_RECEIVED, S.RECEIVED, "delivered"),
        (PurchaseOrderStatus.PARTIALLY_RECEIVED, S.IN_TRANSIT, "dispatched"),
        (PurchaseOrderStatus.QC_PENDING, S.QC_PENDING, "delivered"),
        (PurchaseOrderStatus.GRN_COMPLETED, S.GRN_COMPLETED, "delivered"),
        # A declined order waits for the buyer; it is in "all" only.
        (PurchaseOrderStatus.VENDOR_DECLINED, S.NOT_DISPATCHED, None),
        (PurchaseOrderStatus.CANCELLED, S.NOT_DISPATCHED, None),
    ],
)
def test_each_order_sits_in_one_tab(
    status: PurchaseOrderStatus, tracking: InboundTrackingStatus, tab: str | None
) -> None:
    assert order_tab(status, tracking) == tab


def test_the_tracking_tabs_partition_the_vocabulary_after_dispatch() -> None:
    members = [s for statuses in TAB_TRACKING_STATUSES.values() for s in statuses]
    assert len(members) == len(set(members))
    assert set(members) == set(S) - {S.NOT_DISPATCHED}


# -----------------------------------------------------------------------------
# "PO late"
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "open_quantity", "expected_date", "late"),
    [
        ("ACKNOWLEDGED", Decimal("1"), date(2026, 1, 1), True),
        ("SENT", Decimal("1"), date(2026, 1, 1), True),
        ("PARTIALLY_RECEIVED", Decimal("0.5"), date(2026, 1, 1), True),
        # Due today is not late yet.
        ("ACKNOWLEDGED", Decimal("1"), date(2026, 1, 2), False),
        # Nothing left to deliver.
        ("ACKNOWLEDGED", Decimal("0"), date(2026, 1, 1), False),
        # No planning date, no lateness.
        ("ACKNOWLEDGED", Decimal("1"), None, False),
        # Not receivable: a declined, an unsent and a settled order are not late.
        ("VENDOR_DECLINED", Decimal("1"), date(2026, 1, 1), False),
        ("APPROVED", Decimal("1"), date(2026, 1, 1), False),
        ("CLOSED", Decimal("1"), date(2026, 1, 1), False),
    ],
)
def test_po_late(
    status: str, open_quantity: Decimal, expected_date: date | None, late: bool
) -> None:
    as_of = date(2026, 1, 2)
    for spelled in (status, PurchaseOrderStatus(status)):
        assert (
            po_is_late(
                status=spelled,
                open_quantity=open_quantity,
                expected_date=expected_date,
                as_of=as_of,
            )
            is late
        )


# -----------------------------------------------------------------------------
# Consignment body and message
# -----------------------------------------------------------------------------


def test_a_consignment_needs_a_carrier_and_a_zoned_dispatch_time() -> None:
    with pytest.raises(ValueError, match="transporter or courier"):
        InboundShipmentCreate(shipped_at=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone"):
        InboundShipmentCreate(courier_name="Blue Dart", shipped_at=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="precede dispatch"):
        InboundShipmentCreate(
            courier_name="Blue Dart",
            shipped_at=datetime(2026, 1, 5, tzinfo=UTC),
            expected_delivery_date=date(2026, 1, 4),
        )
    with pytest.raises(ValueError, match="Extra inputs"):
        InboundShipmentCreate.model_validate(
            {
                "courier_name": "Blue Dart",
                "shipped_at": "2026-01-05T00:00:00+00:00",
                "purchase_order_id": "x",
            }
        )


def test_the_shipment_message_carries_the_proposal_fields() -> None:
    order = SimpleNamespace(code="PO-2026-0007")
    shipment = SimpleNamespace(
        shipment_number=2,
        courier_name="Blue Dart",
        tracking_reference="123456789",
        shipped_at=datetime(2026, 10, 12, 9, 30, tzinfo=UTC),
        expected_delivery_date=date(2026, 10, 14),
        packages=5,
        weight_kg=Decimal("70.0000"),
    )
    assert shipment_message(order, shipment, "LR copy uploaded") == (  # type: ignore[arg-type]
        "Shipment 2 for PO-2026-0007: Blue Dart · 123456789 · dispatched 2026-10-12 · "
        "expected 2026-10-14 · 5 boxes · 70 kg · LR copy uploaded"
    )
    bare = SimpleNamespace(
        **{**vars(shipment), "tracking_reference": None, "packages": None, "weight_kg": None}
    )
    assert shipment_message(order, bare, "shipment recorded") == (  # type: ignore[arg-type]
        "Shipment 2 for PO-2026-0007: Blue Dart · tracking pending · dispatched 2026-10-12 · "
        "expected 2026-10-14 · shipment recorded"
    )


# -----------------------------------------------------------------------------
# The PO PDF
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("12345678.9", "1,23,45,678.90"),
        ("100000", "1,00,000.00"),
        ("999.995", "1,000.00"),
        ("0.005", "0.01"),
        ("-1234.5", "-1,234.50"),
        (None, "0.00"),
    ],
)
def test_indian_money(value: str | None, text: str) -> None:
    assert indian_money(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [("20.0000", "20"), ("12.5000", "12.5"), ("0.0000", "0"), ("100", "100")],
)
def test_plain_quantity(value: str, text: str) -> None:
    assert plain_quantity(Decimal(value)) == text


def _pages(payload: bytes) -> int:
    return payload.count(b"/Type /Page") - payload.count(b"/Type /Pages")


def test_the_po_pdf_escapes_markup_and_paginates() -> None:
    line = {
        "code": "RM-<001>",
        "vendor_item_code": "V&1",
        "description": "<b>Lock body</b> & screws",
        "hsn": "8301",
        "quantity": Decimal("10.0000"),
        "uom": "NOS",
        "unit_price": Decimal("12.5"),
        "discount": Decimal("0"),
        "tax": Decimal("22.5"),
        "total": Decimal("147.5"),
    }
    data = {
        "code": "PO-1",
        "po_date": date(2026, 10, 12),
        "status": "SENT",
        "expected_delivery_date": date(2026, 10, 30),
        "vendor_committed_date": date(2026, 11, 2),
        "company": {"name": "A & B <Hardware>", "gstin": "27AAAAA0000A1Z5", "city": "Pune"},
        "vendor": {"name": "Fasteners & Co", "gstin": "27BBBBB0000B1Z5"},
        "currency": "INR",
        "subtotal": Decimal("1000"),
        "total_amount": Decimal("1180"),
        "terms": "Net 30 <days>",
    }
    one_page = render_purchase_order_pdf({**data, "lines": [line]})
    many = render_purchase_order_pdf({**data, "lines": [line] * 80})
    assert one_page.startswith(b"%PDF") and many.startswith(b"%PDF")
    assert _pages(one_page) == 1
    # Eighty lines run past one page; the table repeats its header instead of clipping.
    assert _pages(many) >= 2


def test_the_po_pdf_needs_nothing_but_the_number() -> None:
    assert render_purchase_order_pdf({"code": "PO-1", "lines": []}).startswith(b"%PDF")
