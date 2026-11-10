"""The delivery-challan renderer (E7b/P10.M3, GST Rule 55).

Pure and synchronous by design - it takes the plain mapping the job-work
service assembles and returns bytes - so the shapes that break a statutory
document can be exercised without a database, a broker or a filesystem.
"""

from __future__ import annotations

from src.workers.tasks.reports import (
    JOB_WORK_CHALLAN_FILE_PREFIX,
    _safe_file_name,
    render_job_work_challan_pdf,
)
from tests.job_work_challan_support import document, party

PDF_MAGIC = b"%PDF-"


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


def test_a_challan_is_filed_under_its_own_name_not_the_packing_lists() -> None:
    # The stored name is the download's Content-Disposition: a challan once
    # borrowed the packing list's prefix and was saved as packing-list-JWC-....
    assert (
        _safe_file_name("JWC/2026-27/00007", prefix=JOB_WORK_CHALLAN_FILE_PREFIX)
        == "job-work-challan-JWC-2026-27-00007.pdf"
    )
    assert _safe_file_name("PL/0001", prefix="packing-list") == "packing-list-PL-0001.pdf"
    assert _safe_file_name("SI/0001", prefix="sales-invoice") == "sales-invoice-SI-0001.pdf"


def test_a_number_with_nothing_printable_still_names_the_document() -> None:
    # Separators are replaced and trimmed; an empty number keeps the prefix.
    assert _safe_file_name('../"x"/', prefix="packing-list") == "packing-list-x.pdf"
    assert (
        _safe_file_name("", prefix=JOB_WORK_CHALLAN_FILE_PREFIX) == "job-work-challan-document.pdf"
    )
