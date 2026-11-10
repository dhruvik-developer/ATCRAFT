"""A delivery challan as the job-work service hands it to the PDF renderer.

``document(**overrides)`` is a complete challan mapping and ``party`` one of its
two parties; a test overrides only the field it is about. The renderer's own
tests and the PDF text-security test both start from it.
"""

from __future__ import annotations

from typing import Any


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


__all__ = ["document", "party"]
