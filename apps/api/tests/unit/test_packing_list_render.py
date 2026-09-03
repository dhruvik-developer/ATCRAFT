"""The packing-list renderer.

Pure and synchronous by design - it takes the plain mapping the packing service
assembles and returns bytes - so the shapes that break a report generator can be
exercised without a database, a broker or a filesystem.

The shapes that matter are the degenerate ones. A renderer that only works on a
full document fails at three in the afternoon on the one order somebody packed
into a single empty-looking carton, and it fails inside a Celery worker where
nobody is watching.
"""

from __future__ import annotations

from typing import Any

from src.workers.tasks.reports import CARTONS_PER_PAGE, render_packing_list_pdf

PDF_MAGIC = b"%PDF-"


def carton(number: int, *, contents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "carton_number": number,
        "barcode": f"PK20262700042000{number}X",
        "carton_type": "BOX",
        "gross_weight_kg": "10.5000",
        "net_weight_kg": "10.0000",
        "is_sealed": True,
        "contents": contents
        if contents is not None
        else [
            {
                "item_code": "LOCK-01",
                "item_name": "Premium mortice lock",
                "quantity": "40.0000",
                "batch_number": "B-2026-04",
                "serial_numbers": [],
            }
        ],
    }


def document(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "code": "PK/2026-27/00042",
        "status": "PACKED",
        "planned_date": "2026-09-01",
        "packed_date": "2026-09-01T10:15:00+00:00",
        "customer_code": "CUST-001",
        "customer_name": "Hardware Distributors Ltd",
        "sales_order_code": "SO/2026-27/00311",
        "total_cartons": 1,
        "total_gross_weight": "10.5000",
        "total_net_weight": "10.0000",
        "notes": None,
        "lines": [
            {
                "line_number": 1,
                "item_code": "LOCK-01",
                "item_name": "Premium mortice lock",
                "quantity_to_pack": "40.0000",
                "packed_quantity": "40.0000",
            }
        ],
        "cartons": [carton(1)],
    }
    data.update(overrides)
    return data


def page_count(payload: bytes) -> int:
    """Pages in the produced document, read from the PDF's own page objects."""
    return payload.count(b"/Type /Page\n") or payload.count(b"/Type /Page ")


# -----------------------------------------------------------------------------
# The ordinary document
# -----------------------------------------------------------------------------


def test_a_full_packing_list_renders_to_a_pdf() -> None:
    payload = render_packing_list_pdf(document())
    assert payload.startswith(PDF_MAGIC)
    assert len(payload) > 1000


def test_the_renderer_returns_bytes_not_a_file_handle() -> None:
    # It is handed to ``anyio.to_thread.run_sync`` and its result is hashed and
    # written by the caller; returning an open buffer would leak a descriptor
    # into a worker thread.
    assert isinstance(render_packing_list_pdf(document()), bytes)


def test_rendering_is_deterministic_in_size_for_the_same_document() -> None:
    # Not byte-identical - the footer carries a timestamp - but a document that
    # changed shape between two renders of the same data would mean the layout
    # depends on something outside the payload.
    first = render_packing_list_pdf(document())
    second = render_packing_list_pdf(document())
    assert abs(len(first) - len(second)) < 200


# -----------------------------------------------------------------------------
# Degenerate shapes
# -----------------------------------------------------------------------------


def test_an_order_with_no_cartons_still_renders() -> None:
    # A packing list can legitimately be printed as a pick sheet before any box
    # has been opened.
    payload = render_packing_list_pdf(document(cartons=[], total_cartons=0))
    assert payload.startswith(PDF_MAGIC)


def test_an_order_with_no_lines_still_renders() -> None:
    payload = render_packing_list_pdf(document(lines=[]))
    assert payload.startswith(PDF_MAGIC)


def test_an_empty_carton_still_renders() -> None:
    payload = render_packing_list_pdf(document(cartons=[carton(1, contents=[])]))
    assert payload.startswith(PDF_MAGIC)


def test_an_unpacked_order_with_no_packed_date_still_renders() -> None:
    payload = render_packing_list_pdf(
        document(status="IN_PROGRESS", packed_date=None, sales_order_code=None)
    )
    assert payload.startswith(PDF_MAGIC)


def test_notes_are_included_when_present() -> None:
    with_notes = render_packing_list_pdf(document(notes="Do not stack more than three high."))
    without = render_packing_list_pdf(document())
    assert len(with_notes) > len(without)


# -----------------------------------------------------------------------------
# Pagination
# -----------------------------------------------------------------------------


def test_a_long_packing_list_breaks_between_cartons() -> None:
    # A carton split across a page boundary is exactly the document a
    # goods-inwards clerk mis-reads, so the break is forced between cartons.
    many = document(
        cartons=[carton(n) for n in range(1, CARTONS_PER_PAGE * 3 + 1)],
        total_cartons=CARTONS_PER_PAGE * 3,
    )
    payload = render_packing_list_pdf(many)
    assert page_count(payload) >= 3


def test_a_short_packing_list_fits_on_one_page() -> None:
    payload = render_packing_list_pdf(document())
    assert page_count(payload) == 1


def test_a_carton_holding_thousands_of_serials_prints_a_count_not_the_list() -> None:
    # Printing several thousand serial numbers would make the packing list
    # unusable as a packing list; the values live on the traceability record.
    heavy = document(
        cartons=[
            carton(
                1,
                contents=[
                    {
                        "item_code": "LOCK-01",
                        "item_name": "Premium mortice lock",
                        "quantity": "2000.0000",
                        "batch_number": None,
                        "serial_numbers": [f"SN-{n:05d}" for n in range(2000)],
                    }
                ],
            )
        ]
    )
    payload = render_packing_list_pdf(heavy)
    # Two thousand serials on a single page is only possible because the count
    # is printed rather than the values.
    assert page_count(payload) == 1
