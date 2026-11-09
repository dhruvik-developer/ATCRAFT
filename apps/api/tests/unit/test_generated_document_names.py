"""Every PDF a report task files is named through one helper.

``_safe_file_name`` states a generated document's display name once: what the
document is, then its number with every separator replaced, so
``CN/2026-27/00001`` is stored as ``credit-note-CN-2026-27-00001.pdf``. Three
tasks built their own: the credit note and the vendor debit note kept the
slashes, and the purchase order replaced only those. The storage path never
derives from the name (``build_storage_key``) and ``content_disposition``
sanitises the header, but the stored name is what the Documents screen lists,
and three spellings of one rule drift apart.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.workers.tasks import reports
from src.workers.tasks.reports import _safe_file_name

pytestmark = pytest.mark.unit

REPORT_TASKS = Path(reports.__file__)

#: The prefixes the three tasks that used to name their own PDFs now state.
PREFIXES = ("credit-note", "vendor-debit-note", "purchase-order")


@pytest.mark.parametrize(
    ("prefix", "number", "expected"),
    [
        ("credit-note", "CN/2026-27/00001", "credit-note-CN-2026-27-00001.pdf"),
        ("vendor-debit-note", "DN/WORKER/1", "vendor-debit-note-DN-WORKER-1.pdf"),
        ("purchase-order", "PO/2026-27/00042", "purchase-order-PO-2026-27-00042.pdf"),
    ],
)
def test_a_number_with_separators_names_the_file_without_them(
    prefix: str, number: str, expected: str
) -> None:
    assert _safe_file_name(number, prefix=prefix) == expected


@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_number_with_nothing_printable_still_names_the_document(prefix: str) -> None:
    # A purchase order without a number was stored as "purchase-order-.pdf".
    assert _safe_file_name("", prefix=prefix) == f"{prefix}-document.pdf"
    assert _safe_file_name('/../"', prefix=prefix) == f"{prefix}-document.pdf"


def inline_names(source: str) -> list[str]:
    """Each ``file_name=`` argument spelled inline: a literal, an f-string or a sum.

    A helper's call, or a parameter handed down from one, is the shape every
    generated document is named by.
    """
    return [
        ast.unparse(keyword.value)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "file_name"
        and isinstance(keyword.value, ast.Constant | ast.JoinedStr | ast.BinOp)
    ]


def test_every_generated_document_is_named_by_a_helper() -> None:
    assert inline_names(REPORT_TASKS.read_text(encoding="utf-8")) == []


def test_the_scan_sees_a_name_spelled_inline() -> None:
    source = "\n".join(
        (
            'Document(file_name=f"credit-note-{code}.pdf")',
            'Document(file_name="purchase-order-" + code)',
            'Document(file_name="fixed.pdf")',
            'Document(file_name=_safe_file_name(code, prefix="credit-note"))',
            "Document(file_name=file_name)",
        )
    )

    assert inline_names(source) == [
        "f'credit-note-{code}.pdf'",
        "'purchase-order-' + code",
        "'fixed.pdf'",
    ]
