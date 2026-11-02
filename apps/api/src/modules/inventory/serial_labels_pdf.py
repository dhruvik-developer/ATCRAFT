"""Printable, tenant-safe labels for registered stock serials."""

from __future__ import annotations

import io
from collections.abc import Sequence
from xml.sax.saxutils import escape

from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from src.modules.inventory.schemas import StockSerialRead

# ASSUMPTION: Initial stock uses an A4 sheet with a 3-column by 8-row label grid.
_COLUMNS = 3
_ROWS = 8
_MARGIN = 8 * mm
_GAP_X = 2 * mm
_GAP_Y = 3 * mm
_QR_SIZE = 23 * mm


def render_serial_labels_pdf(serials: Sequence[StockSerialRead]) -> bytes:
    """Render a bounded A4 sheet of 24 labels per page with QR and readable text."""
    if not serials:
        raise ValueError("At least one serial is required to render labels.")

    page_width, page_height = A4
    label_width = (page_width - 2 * _MARGIN - (_COLUMNS - 1) * _GAP_X) / _COLUMNS
    label_height = (page_height - 2 * _MARGIN - (_ROWS - 1) * _GAP_Y) / _ROWS
    buffer = io.BytesIO()
    pdf = Canvas(buffer, pagesize=A4, pageCompression=1)
    title = ParagraphStyle("serial-label-title", fontName="Helvetica-Bold", fontSize=7)
    detail = ParagraphStyle("serial-label-detail", fontName="Helvetica", fontSize=6)
    serial_style = ParagraphStyle(
        "serial-label-number", fontName="Helvetica-Bold", fontSize=8, leading=9
    )

    for index, serial in enumerate(serials):
        page_index = index % (_COLUMNS * _ROWS)
        if index and page_index == 0:
            pdf.showPage()
        column = page_index % _COLUMNS
        row = page_index // _COLUMNS
        x = _MARGIN + column * (label_width + _GAP_X)
        y = page_height - _MARGIN - (row + 1) * label_height - row * _GAP_Y

        pdf.setStrokeColor(colors.lightgrey)
        pdf.setLineWidth(0.35)
        pdf.roundRect(x, y, label_width, label_height, 2 * mm, stroke=1, fill=0)

        qr = createBarcodeDrawing(
            "QR",
            value=serial.serial_number,
            width=_QR_SIZE,
            height=_QR_SIZE,
            barLevel="Q",
        )
        qr.drawOn(pdf, x + 2 * mm, y + (label_height - _QR_SIZE) / 2)

        text_x = x + _QR_SIZE + 4 * mm
        text_width = label_width - _QR_SIZE - 7 * mm
        cursor_y = y + label_height - 4 * mm
        item = escape(serial.item_code or "Stock unit")
        paragraph = Paragraph(item, title)
        _, paragraph_height = paragraph.wrap(text_width, label_height)
        paragraph.drawOn(pdf, text_x, cursor_y - paragraph_height)
        cursor_y -= paragraph_height + 1.5 * mm

        paragraph = Paragraph(escape(serial.serial_number), serial_style)
        _, paragraph_height = paragraph.wrap(text_width, label_height)
        paragraph.drawOn(pdf, text_x, cursor_y - paragraph_height)
        cursor_y -= paragraph_height + 1.5 * mm

        if serial.batch_number:
            paragraph = Paragraph(f"Lot: {escape(serial.batch_number)}", detail)
            _, paragraph_height = paragraph.wrap(text_width, label_height)
            paragraph.drawOn(pdf, text_x, cursor_y - paragraph_height)

    pdf.save()
    return buffer.getvalue()
