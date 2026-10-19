"""Render a purchase-return vendor debit note."""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _text(value: Any, fallback: str = "-") -> str:
    return escape(str(value)) if value not in (None, "") else fallback


def _address(block: dict[str, Any]) -> str:
    values = (
        block.get("address_line1"),
        block.get("address_line2"),
        block.get("city"),
        block.get("state"),
        block.get("postal_code"),
    )
    return ", ".join(escape(str(value)) for value in values if value)


def render_purchase_return_debit_note_pdf(data: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    company = data.get("company") or {}
    vendor = data.get("vendor") or {}
    number = data.get("debit_note_number") or data.get("code")
    story: list[Any] = [
        Paragraph("VENDOR DEBIT NOTE", styles["Title"]),
        Paragraph(f"Debit note: {_text(number)}", styles["Heading2"]),
        Paragraph(
            f"Purchase return: {_text(data.get('code'))} | "
            f"Date: {_text(data.get('return_date'))} | "
            f"Currency: {_text(data.get('currency'), 'INR')}",
            body,
        ),
        Spacer(1, 8),
        Paragraph(f"<b>Issued by:</b> {_text(company.get('name'))}", body),
        Paragraph(f"<b>Vendor:</b> {_text(vendor.get('name'))}", body),
        Paragraph(f"<b>Vendor GSTIN:</b> {_text(vendor.get('gstin'), 'Unregistered')}", body),
    ]
    company_address = _address(company)
    vendor_address = _address(vendor)
    if company_address:
        story.append(Paragraph(f"<b>Company address:</b> {company_address}", body))
    if vendor_address:
        story.append(Paragraph(f"<b>Vendor address:</b> {vendor_address}", body))
    story.extend(
        [
            Paragraph(
                f"Goods receipt: {_text(data.get('goods_receipt_code'))} | "
                f"Purchase order: {_text(data.get('purchase_order_code'))}",
                body,
            ),
            Paragraph(f"<b>Reason:</b> {_text(data.get('reason'))}", body),
            Spacer(1, 8),
        ]
    )

    rows = [["#", "Item / description", "Quantity", "Unit price", "Tax", "Total"]]
    for line in data.get("lines", []):
        rows.append(
            [
                str(line.get("line_number", "-")),
                Paragraph(
                    f"<b>{_text(line.get('item_code'))}</b><br/>{_text(line.get('description'))}",
                    body,
                ),
                f"{line.get('quantity', '-')} {line.get('uom') or ''}".strip(),
                str(line.get("unit_price", "0")),
                str(line.get("tax_amount", "0")),
                str(line.get("line_total", "0")),
            ]
        )
    table = Table(
        rows,
        repeatRows=1,
        colWidths=[10 * mm, 65 * mm, 28 * mm, 27 * mm, 25 * mm, 30 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, "#999999"),
                ("BACKGROUND", (0, 0), (-1, 0), "#E8E8E8"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 8),
            Paragraph(f"Subtotal: {_text(data.get('subtotal'), '0')}", body),
            Paragraph(f"Tax reversed: {_text(data.get('tax_amount'), '0')}", body),
            Paragraph(
                f"Total vendor debit: {_text(data.get('total_amount'), '0')}",
                styles["Heading2"],
            ),
        ]
    )
    SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    ).build(story)
    return output.getvalue()
