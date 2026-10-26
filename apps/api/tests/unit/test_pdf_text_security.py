"""Untrusted document text must never ask ReportLab to load images or URLs."""

import pytest
from reportlab.platypus import paraparser

from src.modules.finance.credit_note_pdf import render_credit_note_pdf
from src.modules.finance.sales_invoice_pdf import render_sales_invoice_pdf
from src.modules.procurement.purchase_return_debit_note_pdf import (
    render_purchase_return_debit_note_pdf,
)
from src.workers.tasks.reports import (
    render_job_work_challan_pdf,
    render_packing_list_pdf,
    render_purchase_order_pdf,
)
from tests.unit.test_job_work_challan_pdf import document as challan_document
from tests.unit.test_packing_list_render import document as packing_document


@pytest.fixture
def no_images(monkeypatch):
    def reject_image(*args, **kwargs):
        raise AssertionError("User-controlled PDF text attempted to load an image")

    monkeypatch.setattr(paraparser, "ImageReader", reject_image)


@pytest.mark.parametrize(
    "source", ["http://127.0.0.1:9/private", "file:///never-read-pdf-test.png"]
)
@pytest.mark.parametrize(
    "field", ["code", "notes", "line_code", "line_name", "carton_code", "carton_name", "barcode"]
)
def test_packing_text_is_literal_and_cannot_load_an_image(no_images, source, field):
    data = packing_document()
    value = f'<img src="{source}"/> & Packing'
    if field in {"code", "notes"}:
        data[field] = value
    elif field.startswith("line_"):
        data["lines"][0]["item_code" if field == "line_code" else "item_name"] = value
    elif field.startswith("carton_"):
        data["cartons"][0]["contents"][0][
            "item_code" if field == "carton_code" else "item_name"
        ] = value
    else:
        data["cartons"][0]["barcode"] = value
    assert render_packing_list_pdf(data).startswith(b"%PDF-")


@pytest.mark.parametrize(
    "source", ["http://127.0.0.1:9/private", "file:///never-read-pdf-test.png"]
)
@pytest.mark.parametrize("renderer", ["credit", "invoice", "debit", "challan", "purchase_order"])
def test_party_names_addresses_and_notes_cannot_load_images_in_other_pdfs(
    no_images, renderer, source
):
    value = f'<img src="{source}"/> & Supplier'
    if renderer == "credit":
        payload = render_credit_note_pdf({"buyer_name": value})
    elif renderer == "invoice":
        payload = render_sales_invoice_pdf(
            {
                "buyer_name": value,
                "seller_name": value,
                "seller_address": value,
                "billing_address": value,
                "notes": value,
                "total_amount": "1.00",
            }
        )
    elif renderer == "debit":
        payload = render_purchase_return_debit_note_pdf(
            {
                "company": {"name": value},
                "vendor": {"name": value, "address_line1": value},
                "reason": value,
            }
        )
    elif renderer == "purchase_order":
        payload = render_purchase_order_pdf(
            {
                "code": value,
                "company": {"name": value},
                "vendor": {"name": value, "address_line1": value},
                "terms": value,
                "lines": [],
            }
        )
    else:
        data = challan_document(notes=value)
        data["consignor"]["legal_name"] = value
        data["consignee"]["address_line1"] = value
        data["lines"][0]["item_name"] = value
        payload = render_job_work_challan_pdf(data)
    assert payload.startswith(b"%PDF-")
