"""Pure A4 tax-invoice rendering and Indian numbering helpers."""

from __future__ import annotations

import io
from collections import defaultdict
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.core.types import to_money

_SMALL = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def _integer_words(number: int) -> str:
    if number < 20:
        return _SMALL[number]
    if number < 100:
        return _TENS[number // 10] + (" " + _SMALL[number % 10] if number % 10 else "")
    for unit, label in (
        (10_000_000, "crore"),
        (100_000, "lakh"),
        (1000, "thousand"),
        (100, "hundred"),
    ):
        if number >= unit:
            quotient, remainder = divmod(number, unit)
            return (
                _integer_words(quotient)
                + " "
                + label
                + (" " + _integer_words(remainder) if remainder else "")
            )
    raise ValueError("Unreachable integer amount")


def amount_in_words(amount: Decimal | str) -> str:
    value = to_money(Decimal(amount))
    if value < 0:
        raise ValueError("An invoice amount cannot be negative")
    rupees = int(value)
    paise = int((value - rupees) * 100)
    text = "Rupees " + _integer_words(rupees)
    if paise:
        text += " and " + _integer_words(paise) + " paise"
    return text + " only"


def indian_digit_grouping(amount: Decimal | str) -> str:
    value = to_money(Decimal(amount))
    sign = "-" if value < 0 else ""
    whole, fraction = format(abs(value), ".2f").split(".")
    if len(whole) > 3:
        tail = whole[-3:]
        prefix = whole[:-3]
        groups: list[str] = []
        while prefix:
            groups.insert(0, prefix[-2:])
            prefix = prefix[:-2]
        whole = ",".join([*groups, tail])
    return f"{sign}{whole}.{fraction}"


def render_sales_invoice_pdf(data: dict[str, Any]) -> bytes:
    """Render only supplied snapshots; this function never reads a master or DB."""
    output = io.BytesIO()
    base = getSampleStyleSheet()
    body = ParagraphStyle("InvoiceBody", parent=base["BodyText"], fontSize=8, leading=11)
    small = ParagraphStyle("InvoiceSmall", parent=body, fontSize=6.5, leading=9)
    right = ParagraphStyle("InvoiceRight", parent=small, alignment=TA_RIGHT)
    title = ParagraphStyle(
        "InvoiceTitle", parent=base["Title"], fontSize=17, leading=21, alignment=TA_CENTER
    )
    heading = ParagraphStyle(
        "InvoiceHeading", parent=body, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4
    )
    width = 180 * mm

    def p(value: Any, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(
            escape(str(value if value is not None else "-")).replace("\n", "<br/>"), style
        )

    def money(value: Any) -> str:
        return indian_digit_grouping(str(value or "0"))

    table_style = TableStyle(
        [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
            ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )
    story: list[Any] = [
        p("TAX INVOICE", title),
        p("Original for Recipient", ParagraphStyle("Recipient", parent=body, alignment=TA_CENTER)),
        Spacer(1, 4 * mm),
    ]
    if data.get("status") == "CANCELLED":
        story.append(p("CANCELLED - retain for statutory records", heading))
    seller = [
        p(data.get("seller_name"), heading),
        p(data.get("seller_address")),
        p(f"GSTIN: {data.get('seller_gstin') or 'Unregistered'}"),
        p(
            f"State: {data.get('seller_state_name') or '-'} "
            f"({data.get('seller_state_code') or '-'})"
        ),
    ]
    meta = [
        p(f"Invoice: {data.get('code', '-')}", heading),
        p(f"Date: {data.get('invoice_date', '-')}"),
        p(f"Due date: {data.get('due_date', '-')}"),
        p(f"Currency: {data.get('currency', 'INR')}"),
        p(f"Place of supply: {data.get('place_of_supply_state_code') or '-'}"),
    ]
    header = Table([[seller, meta]], colWidths=[width * 0.6, width * 0.4])
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(header)
    buyer_meta = (
        f"GSTIN: {data.get('buyer_gstin') or 'Unregistered'} | "
        f"State: {data.get('buyer_state_name') or '-'} "
        f"({data.get('buyer_state_code') or '-'})"
    )
    parties = Table(
        [
            [
                [
                    p("Bill to", heading),
                    p(data.get("buyer_name")),
                    p(data.get("billing_address")),
                    p(buyer_meta),
                ],
                [
                    p("Ship to", heading),
                    p(data.get("buyer_name")),
                    p(data.get("shipping_address")),
                    p(
                        "Place of supply state code: "
                        f"{data.get('place_of_supply_state_code') or '-'}"
                    ),
                ],
            ]
        ],
        colWidths=[width / 2, width / 2],
    )
    parties.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.35, colors.grey)]
        )
    )
    story += [parties, Spacer(1, 4 * mm)]
    labels = [
        "Sr",
        "Description",
        "HSN",
        "Qty",
        "UoM",
        "Rate",
        "Discount",
        "Taxable",
        "IGST" if data.get("is_interstate") else "CGST / SGST",
        "Total",
    ]
    rows: list[list[Any]] = [[p(label, small) for label in labels]]
    hsn: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for line in data.get("lines", []):
        rate = Decimal(str(line["tax_rate"]))
        if data.get("is_interstate"):
            tax = f"{rate.normalize()}%: {money(line['igst_amount'])}"
        else:
            half = (rate / 2).normalize()
            tax = (
                f"CGST {half}%: {money(line['cgst_amount'])}\n"
                f"SGST {half}%: {money(line['sgst_amount'])}"
            )
        rows.append(
            [
                p(line["line_number"], small),
                p(line["description"], small),
                p(line.get("hsn_code"), small),
                p(str(line["quantity"]), right),
                p(line["uom_code"], small),
                p(money(line["unit_price"]), right),
                p(money(line["discount_amount"]), right),
                p(money(line["taxable_amount"]), right),
                p(tax, small),
                p(money(line["line_total"]), right),
            ]
        )
        slab = hsn[(line.get("hsn_code") or "-", str(rate.normalize()))]
        for field in ("taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "tax_amount"):
            slab[field] += Decimal(str(line[field]))
    items = Table(
        rows,
        colWidths=[value * mm for value in (7, 34, 13, 11, 9, 17, 15, 20, 31, 23)],
        repeatRows=1,
        hAlign="LEFT",
    )
    items.setStyle(table_style)
    story += [items, p("HSN-wise tax summary (INR)", heading)]
    summary_rows: list[list[Any]] = [
        [
            p(label, small)
            for label in ("HSN", "GST rate", "Taxable", "CGST", "SGST", "IGST", "Total tax")
        ]
    ]
    for (code, slab_rate), slab in sorted(hsn.items()):
        summary_rows.append(
            [
                p(code, small),
                p(slab_rate + "%", small),
                *[
                    p(money(slab[field]), right)
                    for field in (
                        "taxable_amount",
                        "cgst_amount",
                        "sgst_amount",
                        "igst_amount",
                        "tax_amount",
                    )
                ],
            ]
        )
    summary = Table(summary_rows, colWidths=[width / 7] * 7, repeatRows=1)
    summary.setStyle(table_style)
    story.append(summary)
    totals = [
        [p(label), p("INR " + money(data.get(field)), right)]
        for label, field in (
            ("Gross subtotal", "subtotal"),
            ("Discount", "discount_amount"),
            ("Taxable value", "taxable_amount"),
            ("Total GST", "tax_amount"),
            ("Round off", "round_off_amount"),
            ("Invoice total", "total_amount"),
        )
    ]
    totals_table = Table(totals, colWidths=[55 * mm, 40 * mm], hAlign="RIGHT")
    totals_table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.grey),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ]
        )
    )
    story += [
        Spacer(1, 3 * mm),
        totals_table,
        p(amount_in_words(str(data["total_amount"])), heading),
    ]
    bank = [p("Bank details", heading)]
    for label, field in (
        ("Bank", "bank_name"),
        ("Account name", "bank_account_name"),
        ("Account number", "bank_account_number"),
        ("IFSC", "bank_ifsc"),
        ("Branch", "bank_branch"),
    ):
        if data.get(field):
            bank.append(p(f"{label}: {data[field]}"))
    transport = [p("Transport", heading)]
    for label, field in (
        ("Carrier", "transporter_name"),
        ("Vehicle", "vehicle_number"),
        ("LR number", "lr_number"),
        ("E-way bill", "eway_bill_number"),
    ):
        if data.get(field):
            transport.append(p(f"{label}: {data[field]}"))
    footer = Table([[bank, transport]], colWidths=[width / 2, width / 2])
    footer.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.35, colors.grey)]
        )
    )
    story.append(footer)
    if data.get("invoice_terms"):
        story += [p("Terms", heading), p(data["invoice_terms"])]
    if data.get("notes"):
        story += [p("Notes", heading), p(data["notes"])]
    story += [
        Spacer(1, 6 * mm),
        p(
            f"For {data['seller_name']}",
            ParagraphStyle("Signatory", parent=body, alignment=TA_RIGHT),
        ),
        Spacer(1, 8 * mm),
        p("Authorised Signatory", ParagraphStyle("Authorised", parent=body, alignment=TA_RIGHT)),
    ]

    def page_footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawRightString(
            A4[0] - 15 * mm, 10 * mm, f"{data.get('code', 'Tax invoice')} | Page {document.page}"
        )
        canvas.restoreState()

    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=16 * mm,
        title=f"Tax invoice {data.get('code', '')}",
        author=str(data.get("seller_name", "")),
    )
    document.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return output.getvalue()
