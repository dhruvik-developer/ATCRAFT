"""A packing list as the packing service hands it to the PDF renderer.

``document(**overrides)`` is a complete one-carton packing list and
``carton(number)`` one more carton for it; a test overrides only what it is
about. The renderer's own tests and the PDF text-security test both start from
it.
"""

from __future__ import annotations

from typing import Any


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


__all__ = ["carton", "document"]
