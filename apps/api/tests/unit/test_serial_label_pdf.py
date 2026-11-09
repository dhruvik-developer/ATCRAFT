"""Serial labels render as literal, scannable PDF content, off the event loop."""

import asyncio
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from reportlab.platypus import paraparser

from src.core.deps import TenantContext
from src.modules.inventory import service as inventory_service
from src.modules.inventory.models import SerialStatus
from src.modules.inventory.schemas import StockSerialRead
from src.modules.inventory.serial_labels_pdf import render_serial_labels_pdf

pytestmark = pytest.mark.unit


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


async def test_a_label_sheet_is_drawn_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sheet is seconds of pure-Python QR drawing; the loop must keep serving.

    The renderer is replaced by one that takes 200 ms and notes which thread it
    ran on. A ticker on the loop measures the longest gap between its wake-ups
    while the sheet is drawn: rendered inline, that gap is the whole 200 ms.
    """
    serial_id = uuid4()
    loop_thread = threading.get_ident()
    rendered_on: list[int] = []

    def slow_render(reads: Sequence[Any]) -> bytes:
        rendered_on.append(threading.get_ident())
        time.sleep(0.2)
        return b"%PDF-labels for " + str(len(reads)).encode()

    async def visible_by_ids(ids: Sequence[UUID]) -> dict[UUID, Any]:
        return {wanted: object() for wanted in ids}

    async def serial_reads(rows: Sequence[Any]) -> list[Any]:
        return list(rows)

    monkeypatch.setattr(inventory_service, "render_serial_labels_pdf", slow_render)
    traceability = inventory_service.TraceabilityService(
        session=None,  # type: ignore[arg-type]
        tenant=TenantContext(company_id=uuid4(), user_id=uuid4()),
    )
    monkeypatch.setattr(traceability.serials, "visible_by_ids", visible_by_ids)
    monkeypatch.setattr(traceability, "_serial_reads", serial_reads)

    longest_gap = 0.0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal longest_gap
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            longest_gap = max(longest_gap, now - last)
            last = now

    ticking = asyncio.create_task(ticker())
    try:
        pdf = await traceability.serial_labels_pdf([serial_id])
    finally:
        done.set()
        await ticking

    assert pdf == b"%PDF-labels for 1"
    assert rendered_on and rendered_on[0] != loop_thread
    assert longest_gap < 0.1, f"the event loop stalled for {longest_gap:.3f}s"
