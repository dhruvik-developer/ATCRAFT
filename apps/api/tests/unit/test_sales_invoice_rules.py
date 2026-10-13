"""Money, numbering and lifecycle rules shared by real invoice transactions."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.common.codes import DocTypes
from src.core.permissions import Permission
from src.modules.finance.models import SalesInvoice, SalesInvoiceStatus, SystemAccount
from src.modules.finance.sales_invoice_pdf import (
    amount_in_words,
    indian_digit_grouping,
    render_sales_invoice_pdf,
)
from src.modules.finance.sales_invoice_service import (
    SALES_INVOICE_LIFECYCLE,
    apportion_discount,
    sales_invoice_journal_lines,
    sales_invoice_status_after_payment,
)
from src.modules.finance.schemas import AllocationRequest, PaymentAllocationIn
from src.modules.finance.service import assert_balanced, first_free_account_code
from src.modules.sales.models import SalesOrderStatus
from src.modules.sales.service import SALES_ORDER_LIFECYCLE, dispatchable_quantity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("0", "Rupees zero only"),
        ("0.50", "Rupees zero and fifty paise only"),
        ("1.01", "Rupees one and one paise only"),
        ("100", "Rupees one hundred only"),
        ("1001.10", "Rupees one thousand one and ten paise only"),
        ("100000", "Rupees one lakh only"),
        ("10000000", "Rupees one crore only"),
        (
            "999999999.99",
            "Rupees ninety nine crore ninety nine lakh ninety nine thousand "
            "nine hundred ninety nine and ninety nine paise only",
        ),
        ("1.995", "Rupees two only"),
    ],
)
def test_indian_amount_in_words(amount, words):
    assert amount_in_words(amount) == words


@pytest.mark.parametrize(
    ("amount", "grouped"),
    [
        ("0", "0.00"),
        ("123", "123.00"),
        ("1234.5", "1,234.50"),
        ("1234567.89", "12,34,567.89"),
        ("999999999.99", "99,99,99,999.99"),
        ("-1000.5", "-1,000.50"),
    ],
)
def test_indian_grouping(amount, grouped):
    assert indian_digit_grouping(amount) == grouped


def test_negative_amount_in_words_refused():
    with pytest.raises(ValueError):
        amount_in_words("-1")


def test_last_invoice_discount_takes_rounding_remainder():
    shares = []
    for index in range(3):
        shares.append(
            apportion_discount(
                total_discount=Decimal("1"),
                ordered=Decimal("3"),
                quantity=Decimal("1"),
                invoiced_before=Decimal(index),
                discount_before=sum(shares, Decimal("0")),
            )
        )
    assert shares == [Decimal(".33"), Decimal(".33"), Decimal(".34")]


@pytest.mark.parametrize("round_off", ["-.49", "0", ".49"])
def test_invoice_journal_balances_on_both_rounding_sides(round_off):
    rounding = Decimal(round_off)
    invoice = SalesInvoice(
        customer_id=uuid.uuid4(),
        taxable_amount=Decimal("100"),
        tax_amount=Decimal("18"),
        round_off_amount=rounding,
        total_amount=Decimal("118") + rounding,
    )
    accounts = {str(code): SimpleNamespace(id=uuid.uuid4()) for code in SystemAccount}
    assert assert_balanced(sales_invoice_journal_lines(invoice, accounts)).is_balanced


@pytest.mark.parametrize(
    ("paid", "status"),
    [
        ("0", SalesInvoiceStatus.ISSUED),
        ("1", SalesInvoiceStatus.PARTIALLY_PAID),
        ("100", SalesInvoiceStatus.PAID),
    ],
)
def test_status_tracks_payment(paid, status):
    assert (
        sales_invoice_status_after_payment(total_amount=Decimal("100"), paid_amount=Decimal(paid))
        == status
    )


def test_both_machines_validate_and_new_edges_have_the_expected_right():
    SALES_INVOICE_LIFECYCLE.validate()
    SALES_ORDER_LIFECYCLE.validate()
    edge = SALES_ORDER_LIFECYCLE.find(SalesOrderStatus.INVOICED, SalesOrderStatus.DISPATCHED)
    assert edge and edge.permission == Permission.SALES_INVOICE_CANCEL
    assert not SALES_INVOICE_LIFECYCLE.allowed_targets(SalesInvoiceStatus.CANCELLED)
    assert SALES_ORDER_LIFECYCLE.can_transition(
        SalesOrderStatus.ALLOCATED, SalesOrderStatus.CONFIRMED
    )
    assert SALES_ORDER_LIFECYCLE.can_transition(
        SalesOrderStatus.CONFIRMED, SalesOrderStatus.PARTIALLY_DISPATCHED
    )


@pytest.mark.parametrize(
    ("reserved", "planned", "expected"),
    [("200", "0", "200"), ("200", "400", "100"), ("0", "0", "0"), ("800", "0", "500")],
)
def test_live_reservation_caps_dispatch(reserved, planned, expected):
    assert dispatchable_quantity(
        ordered=Decimal("500"),
        dispatched=Decimal("0"),
        reserved=Decimal(reserved),
        planned=Decimal(planned),
    ) == Decimal(expected)


def test_first_free_system_code_preserves_client_chart():
    assert first_free_account_code("4500", {"4500", "4501", "4000"}) == "4502"
    assert first_free_account_code("4500", {"4501"}) == "4500"
    assert len(f"{DocTypes.SALES_INVOICE.prefix}/2026-27/00042") == 16


def test_polymorphic_allocation_requires_exactly_one_and_allows_explicit_release():
    with pytest.raises(ValidationError):
        PaymentAllocationIn(amount=Decimal("1"))
    with pytest.raises(ValidationError):
        PaymentAllocationIn(
            amount=Decimal("1"), vendor_invoice_id=uuid.uuid4(), sales_invoice_id=uuid.uuid4()
        )
    assert AllocationRequest(allocations=[], replace=True).allocations == []
    with pytest.raises(ValidationError):
        AllocationRequest(allocations=[])


def sample_invoice_data():
    return {
        "code": "SI/2026-27/00042",
        "seller_name": "Atcraft Manufacturing Private Limited",
        "seller_address": "Plot 42, Industrial Estate, Pune, Maharashtra 411001, India",
        "seller_gstin": "27AAAAA0000A1Z5",
        "seller_state_code": "27",
        "seller_state_name": "Maharashtra",
        "buyer_name": "Fictional Hardware Distributors",
        "buyer_gstin": "27BBBBB1111B1Z4",
        "buyer_state_code": "27",
        "buyer_state_name": "Maharashtra",
        "billing_address": "18 Market Road, Pune, Maharashtra 411002",
        "shipping_address": "Warehouse 8, Pune, Maharashtra 411003",
        "place_of_supply_state_code": "27",
        "is_interstate": False,
        "invoice_date": "2026-10-12",
        "due_date": "2026-11-11",
        "currency": "INR",
        "status": "ISSUED",
        "subtotal": "2500.00",
        "discount_amount": "33.33",
        "taxable_amount": "2466.67",
        "cgst_amount": "222.00",
        "sgst_amount": "222.00",
        "igst_amount": "0",
        "tax_amount": "444.00",
        "round_off_amount": ".33",
        "total_amount": "2911.00",
        "bank_name": "Fictional Foundry Bank",
        "bank_account_name": "Atcraft Manufacturing Private Limited",
        "bank_account_number": "000001234567",
        "bank_ifsc": "FICT0001234",
        "bank_branch": "Pune Industrial Estate",
        "invoice_terms": "Payment within 30 days. Goods supplied under agreed sales-order terms.",
        "transporter_name": "Fictional Road Carriers",
        "vehicle_number": "MH12AB1234",
        "lr_number": "LR-12042",
        "eway_bill_number": "123456789012",
        "lines": [
            {
                "line_number": 1,
                "description": "Architectural hinge - satin finish, heavy duty 125 mm",
                "hsn_code": "8302",
                "quantity": "250",
                "uom_code": "NOS",
                "unit_price": "10",
                "discount_amount": "33.33",
                "taxable_amount": "2466.67",
                "tax_rate": "18",
                "cgst_amount": "222",
                "sgst_amount": "222",
                "igst_amount": "0",
                "tax_amount": "444",
                "line_total": "2910.67",
            }
        ],
    }


def test_invoice_pdf_builds_and_multiline_tables_paginate():
    data = sample_invoice_data()
    assert render_sales_invoice_pdf(data).startswith(b"%PDF")
    data["lines"] = [{**data["lines"][0], "line_number": number} for number in range(1, 61)]
    assert render_sales_invoice_pdf(data).startswith(b"%PDF")
