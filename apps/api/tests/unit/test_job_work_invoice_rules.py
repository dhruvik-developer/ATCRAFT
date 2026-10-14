"""Job-work invoice matching, chart provisioning and payload coherence.

A job-work bill is a vendor invoice of type ``JOB_WORK``. It shares payments,
approval and the payables ledger with a purchase bill, but it is matched
against a job-work order and its receipt lines: price against the order's
``rate_per_unit``, quantity against the good ``output_quantity``, and the billed
amount against the still-unbilled charges. Billing beyond those charges is a
hard block, not a variance - no override can approve billing the same charges
twice.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.modules.finance.models import JOB_WORK_ALLOW_FREE_LINES, MatchStatus, VendorInvoiceType
from src.modules.finance.schemas import VendorInvoiceCreate, VendorInvoiceLineIn
from src.modules.finance.service import (
    MatchLineInput,
    evaluate_match,
    pick_free_chart_code,
)

pytestmark = pytest.mark.unit

INVOICE_ID = uuid.uuid4()
VENDOR_ID = uuid.uuid4()
ORDER_ID = uuid.uuid4()
RECEIPT_LINE_ID = uuid.uuid4()


def jw_line(
    *,
    invoiced_quantity: str,
    invoiced_unit_price: str,
    rate_per_unit: str = "18.50",
    rate_basis: str = "PER_PIECE",
    output_quantity: str = "58",
    unbilled_charges: str = "1073.00",
    receipt_line: bool = True,
    line_number: int = 1,
) -> MatchLineInput:
    quantity = Decimal(invoiced_quantity)
    price = Decimal(invoiced_unit_price)
    return MatchLineInput(
        line_number=line_number,
        invoice_line_id=uuid.uuid4(),
        invoiced_quantity=quantity,
        invoiced_unit_price=price,
        invoiced_amount=quantity * price,
        job_work_receipt_line_id=uuid.uuid4() if receipt_line else None,
        job_work_rate_per_unit=Decimal(rate_per_unit),
        job_work_rate_basis=rate_basis,
        job_work_output_quantity=Decimal(output_quantity),
        job_work_unbilled_charges=Decimal(unbilled_charges),
    )


def run(*lines: MatchLineInput, **overrides: object):
    kwargs: dict = {
        "invoice_id": INVOICE_ID,
        "invoice_code": "VINV/2026-27/00009",
        "vendor_id": VENDOR_ID,
        "purchase_order_id": None,
        "goods_receipt_id": None,
        "invoice_type": VendorInvoiceType.JOB_WORK,
        "job_work_order_id": ORDER_ID,
    }
    kwargs.update(overrides)
    return evaluate_match(list(lines), **kwargs)


class TestJobWorkAgreement:
    def test_a_bill_matching_rate_output_and_unbilled_charges_passes(self) -> None:
        result = run(
            jw_line(invoiced_quantity="58", invoiced_unit_price="18.50"),
        )

        assert result.matched is True
        assert result.match_status is MatchStatus.MATCHED
        assert result.blocking_reasons == []
        assert result.has_overbilling is False
        assert result.job_work_order_id == ORDER_ID
        assert result.total_expected == Decimal("1073.00")
        assert result.lines[0].job_work_receipt_line_id is not None

    def test_no_purchase_order_is_needed_for_a_job_work_bill(self) -> None:
        result = run(jw_line(invoiced_quantity="58", invoiced_unit_price="18.50"))

        assert not any("purchase order" in reason for reason in result.blocking_reasons)

    def test_a_partial_bill_within_the_unbilled_charges_passes(self) -> None:
        # 40 of 58 pieces at the agreed rate: the remainder stays billable.
        result = run(
            jw_line(
                invoiced_quantity="40",
                invoiced_unit_price="18.50",
                unbilled_charges="1073.00",
            )
        )

        assert result.matched is True
        assert result.has_overbilling is False


class TestJobWorkPrice:
    def test_a_price_beyond_two_percent_blocks_but_is_overridable(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="58",
                invoiced_unit_price="19.00",
                unbilled_charges="5000.00",
            )
        )

        assert result.matched is False
        assert result.has_overbilling is False
        assert any("differs from the agreed" in reason for reason in result.blocking_reasons)

    def test_a_price_within_two_percent_passes(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="58",
                invoiced_unit_price="18.80",
                unbilled_charges="5000.00",
            )
        )

        assert result.matched is True


class TestJobWorkQuantity:
    def test_billing_more_than_the_good_output_blocks(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="60",
                invoiced_unit_price="18.50",
                output_quantity="58",
                unbilled_charges="5000.00",
            )
        )

        assert result.matched is False
        assert any("good output" in reason for reason in result.blocking_reasons)

    def test_a_line_with_no_receipt_link_blocks(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="10",
                invoiced_unit_price="18.50",
                receipt_line=False,
            )
        )

        assert result.matched is False
        assert any(
            "not billed against any job-work receipt line" in reason
            for reason in result.blocking_reasons
        )


class TestOverbillingIsAHardBlock:
    def test_billing_beyond_the_unbilled_charges_is_flagged(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="58",
                invoiced_unit_price="18.50",
                unbilled_charges="740.00",
            )
        )

        assert result.matched is False
        assert result.has_overbilling is True
        assert any("refused" in reason for reason in result.blocking_reasons)

    def test_a_lot_charge_billed_twice_is_refused(self) -> None:
        # A LOT rate is earned once per order: the second receipt line carries
        # no charges, so any bill against it is an over-bill.
        result = run(
            jw_line(
                invoiced_quantity="1",
                invoiced_unit_price="5000",
                rate_per_unit="5000",
                rate_basis="LOT",
                output_quantity="100",
                unbilled_charges="0.00",
            )
        )

        assert result.matched is False
        assert result.has_overbilling is True

    def test_a_lot_charge_billed_once_passes(self) -> None:
        result = run(
            jw_line(
                invoiced_quantity="1",
                invoiced_unit_price="5000",
                rate_per_unit="5000",
                rate_basis="LOT",
                output_quantity="100",
                unbilled_charges="5000.00",
            )
        )

        assert result.matched is True
        assert result.has_overbilling is False


class TestChartCodePicker:
    def test_a_free_preferred_code_is_used_as_is(self) -> None:
        assert pick_free_chart_code("5150", {"1100", "2100"}) == "5150"

    def test_a_taken_code_walks_upward_to_the_next_free_one(self) -> None:
        assert pick_free_chart_code("5150", {"5150", "5151"}) == "5152"

    def test_a_code_taken_far_upward_still_lands_nearby(self) -> None:
        taken = {str(code) for code in range(5150, 5160)}
        assert pick_free_chart_code("5150", taken) == "5160"


def _payload(**overrides: object) -> dict:
    base: dict = {
        "vendor_id": uuid.uuid4(),
        "vendor_invoice_number": "CHR/2026/0007",
        "invoice_date": "2026-10-01",
        "invoice_type": "JOB_WORK",
        "job_work_order_id": str(ORDER_ID),
        "lines": [
            {
                "job_work_receipt_line_id": str(RECEIPT_LINE_ID),
                "description": "Antique brass plating - 58 lever sets",
                "hsn_code": "9988",
                "quantity": "58",
                "unit_price": "18.50",
                "tax_rate": "18",
            }
        ],
    }
    base.update(overrides)
    return base


def _po_line() -> dict:
    return {
        "purchase_order_line_id": str(uuid.uuid4()),
        "description": "Brass rod",
        "quantity": "10",
        "unit_price": "25",
    }


class TestJobWorkPayloadCoherence:
    def test_a_job_work_bill_needs_its_order(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="must name the job-work order"):
            VendorInvoiceCreate(**_payload(job_work_order_id=None))

    def test_a_purchase_bill_cannot_name_a_job_work_order(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="Only a job-work invoice"):
            VendorInvoiceCreate(**_payload(invoice_type="PURCHASE"))

    def test_a_job_work_bill_cannot_name_a_purchase_order(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="not against a purchase order"):
            VendorInvoiceCreate(**_payload(purchase_order_id=str(uuid.uuid4())))

    def test_every_line_must_name_a_receipt_line_by_default(self) -> None:
        from pydantic import ValidationError

        assert JOB_WORK_ALLOW_FREE_LINES is False
        payload = _payload()
        payload["lines"] = [
            {
                "description": "Freight to the plater",
                "quantity": "1",
                "unit_price": "500",
            }
        ]
        with pytest.raises(ValidationError, match="must name the job-work receipt"):
            VendorInvoiceCreate(**payload)

    def test_a_job_work_line_cannot_also_name_a_purchase_order_line(self) -> None:
        from pydantic import ValidationError

        line = _po_line()
        line["job_work_receipt_line_id"] = str(RECEIPT_LINE_ID)
        with pytest.raises(ValidationError, match="cannot also name"):
            VendorInvoiceLineIn(**line)

    def test_a_purchase_line_cannot_name_a_job_work_receipt_line(self) -> None:
        from pydantic import ValidationError

        payload = _payload(invoice_type="PURCHASE", job_work_order_id=None)
        payload["lines"] = [
            {
                "job_work_receipt_line_id": str(RECEIPT_LINE_ID),
                "description": "Antique brass plating - 58 lever sets",
                "quantity": "58",
                "unit_price": "18.50",
            }
        ]
        with pytest.raises(ValidationError, match="on a purchase invoice"):
            VendorInvoiceCreate(**payload)
