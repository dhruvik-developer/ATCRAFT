"""Read what a PDF renderer produced, for the tests of the rendered documents.

The delivery-challan and packing-list renderers return bytes, and their tests
ask the same two questions of them: is this a PDF at all, and how many pages did
it lay out. Both answers come from the document itself - the ``%PDF-`` header,
and the page objects reportlab writes - and the two suites carried identical
copies of the reader until it moved here.

``test_vendor_portal_rules._pages`` counts by another rule - every
``/Type /Page`` less the ``/Type /Pages`` tree nodes - and stays with its test.
"""

from __future__ import annotations

#: The header every PDF file opens with.
PDF_MAGIC = b"%PDF-"


def page_count(payload: bytes) -> int:
    """Pages in the produced document, read from the PDF's own page objects."""
    return payload.count(b"/Type /Page\n") or payload.count(b"/Type /Page ")


__all__ = ["PDF_MAGIC", "page_count"]
