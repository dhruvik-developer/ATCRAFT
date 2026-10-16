"""The delivery-challan renderer (E7b/P10.M3, GST Rule 55).

Pure and synchronous by design - it takes the plain mapping the job-work
service assembles and returns bytes - so the shapes that break a statutory
document can be exercised without a database, a broker or a filesystem.
"""

from __future__ import annotations

from typing import Any

from src.workers.tasks.reports import render_job_work_challan_pdf

PDF_MAGIC = b"%PDF-"


def party(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Atcraft Manufacturing",
        "legal_name": "Atcraft Manufacturing Pvt Ltd",
        "address_line1": "Plot 42, GIDC Industrial Estate",
        "address_line2": None,
        "city": "Ahmedabad",
        "state": "Gujarat",
        "postal_code": "382044",
        "gstin": "24ABCDE1234F1Z5",
    }
    data.update(overrides)
    return data


def document(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "code": "JWC/2026-27/00007",
        "challan_date": "2026-09-05",
        "expected_return_date": "2026-09-20",
        "order_code": "JW/2026-27/00012",
        "process_type": "PVD",
        "process_description": "PVD, rose gold, 3 micron",
        "purpose": "Sent for job work \u2014 not for sale",
        "consignor": party(),
        "consignee": party(
            name="Chrome Plating Works",
            legal_name="Chrome Plating Works",
            address_line1="Shed 7, Vatva",
            city="Ahmedabad",
            state="Gujarat",
            postal_code="382445",
            gstin="24ABCDE5678G1Z5",
        ),
        "vehicle_number": "GJ01AB1234",
        "transporter": {"code": "TR-01", "name": "Swift Carriers"},
        "lines": [
            {
                "line_number": 1,
                "item_code": "HANDLE-RAW-01",
                "item_name": "Raw brass handle",
                "hsn_code": "8302",
                "uom": "NOS",
                "quantity": "100.0000",
                "unit_value": "100.000000",
                "total_value": "10000.00",
            }
        ],
        "total_quantity": "100.0000",
        "total_value": "10000.00",
        "notes": None,
    }
    data.update(overrides)
    return data


def page_count(payload: bytes) -> int:
    """Pages in the produced document, read from the PDF's own page objects."""
    return payload.count(b"/Type /Page\n") or payload.count(b"/Type /Page ")


def test_a_full_challan_renders_to_a_pdf() -> None:
    payload = render_job_work_challan_pdf(document())
    assert payload.startswith(PDF_MAGIC)
    assert len(payload) > 1000
    assert page_count(payload) == 1


def test_a_challan_without_optional_parties_still_renders() -> None:
    payload = render_job_work_challan_pdf(
        document(
            transporter=None,
            vehicle_number=None,
            notes=None,
            consignor=party(legal_name=None, gstin=None),
            lines=[
                {
                    "line_number": 1,
                    "item_code": "HANDLE-RAW-01",
                    "item_name": "",
                    "hsn_code": None,
                    "uom": "",
                    "quantity": "10.0000",
                    "unit_value": "100.000000",
                    "total_value": "1000.00",
                }
            ],
        )
    )
    assert payload.startswith(PDF_MAGIC)


def test_markup_in_master_data_cannot_break_the_document() -> None:
    payload = render_job_work_challan_pdf(
        document(consignor=party(name="Acme <b>Works</b> & Sons"))
    )
    assert payload.startswith(PDF_MAGIC)


def test_rendering_is_stable_for_the_same_document() -> None:
    # Not byte-identical - the footer carries a timestamp - but a document that
    # changed shape between two renders of the same data would mean the layout
    # depends on something outside the payload.
    first = render_job_work_challan_pdf(document())
    second = render_job_work_challan_pdf(document())
    assert abs(len(first) - len(second)) < 200
