from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def render_credit_note_pdf(data: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    story: list[Any] = [
        Paragraph("CREDIT NOTE", styles["Title"]),
        Paragraph(f"Credit note: {escape(str(data.get('code', '-')))}", styles["Heading2"]),
        Paragraph(
            f"Date: {escape(str(data.get('note_date', '-')))} | "
            f"Currency: {escape(str(data.get('currency', 'INR')))}",
            body,
        ),
        Spacer(1, 8),
        Paragraph(f"Customer: {escape(str(data.get('buyer_name', '-')))}", body),
        Spacer(1, 8),
    ]
    rows = [["Item", "Quantity", "Taxable", "Tax", "Total"]]
    for line in data.get("lines", []):
        rows.append(
            [
                str(line.get("hsn_code") or line.get("item_id") or "-"),
                str(line.get("quantity") or "-"),
                str(line.get("taxable_amount", "0")),
                str(line.get("tax_amount", "0")),
                str(line.get("line_total", "0")),
            ]
        )
    table = Table(rows, repeatRows=1, colWidths=[55 * mm, 25 * mm, 30 * mm, 25 * mm, 30 * mm])
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, "#999999"),
                ("BACKGROUND", (0, 0), (-1, 0), "#E8E8E8"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 8),
            Paragraph(f"Taxable amount: {escape(str(data.get('taxable_amount', '0')))}", body),
            Paragraph(f"Tax amount: {escape(str(data.get('tax_amount', '0')))}", body),
            Paragraph(f"Round-off: {escape(str(data.get('round_off_amount', '0')))}", body),
            Paragraph(
                f"Total amount: {escape(str(data.get('total_amount', '0')))}", styles["Heading2"]
            ),
        ]
    )
    SimpleDocTemplate(output, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm).build(story)
    return output.getvalue()
