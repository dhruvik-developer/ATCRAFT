"""Serial labels render as literal, scannable PDF content."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from reportlab.platypus import paraparser

from src.modules.inventory.models import SerialStatus
from src.modules.inventory.schemas import StockSerialRead
from src.modules.inventory.serial_labels_pdf import render_serial_labels_pdf


def test_serial_label_pdf_handles_unicode_and_escapes_catalogue_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_image(*args, **kwargs):
        raise AssertionError("Serial label text attempted to load an image")

    monkeypatch.setattr(paraparser, "ImageReader", reject_image)
    serial = StockSerialRead(
        id=uuid4(),
        item_id=uuid4(),
        serial_number="SN-雪-0001",
        batch_id=None,
        warehouse_id=None,
        location_id=None,
        status=SerialStatus.IN_STOCK,
        received_on=None,
        unit_cost="0",
        notes=None,
        created_at=datetime.now(UTC),
        item_code='<img src="file:///not-read"/>',
        item_name="Ignored by label",
    )

    pdf = render_serial_labels_pdf([serial])

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1_000
