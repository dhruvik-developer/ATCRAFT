"""Fiscal totals are explicit and customer text cannot become ReportLab markup."""

from reportlab.platypus import Paragraph as ReportParagraph

from src.modules.finance import credit_note_pdf


def test_credit_note_renders_stored_round_off_and_escapes_customer_markup(monkeypatch):
    paragraphs = []

    def paragraph(text, *args, **kwargs):
        paragraphs.append(text)
        return ReportParagraph(text, *args, **kwargs)

    monkeypatch.setattr(credit_note_pdf, "Paragraph", paragraph)
    data = {
        "code": "CN/2026-27/00001",
        "note_date": "2026-10-25",
        "currency": "INR",
        "buyer_name": '<img src="file:///never-read-credit-note-test.png"/> & Partners',
        "taxable_amount": "1.00",
        "tax_amount": "0.18",
        "round_off_amount": "-0.18",
        "total_amount": "1.00",
        "lines": [],
    }
    pdf = credit_note_pdf.render_credit_note_pdf(data)
    assert pdf.startswith(b"%PDF-")
    assert "Round-off: -0.18" in paragraphs
    assert "Taxable amount: 1.00" in paragraphs
    assert "Tax amount: 0.18" in paragraphs
    assert "Total amount: 1.00" in paragraphs
    assert any("&lt;img" in text and "&amp; Partners" in text for text in paragraphs)
