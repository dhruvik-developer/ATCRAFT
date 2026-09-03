"""Document number generation.

Every business document needs a human-usable identifier (``PO/2026-27/00042``)
alongside its UUID. Getting this wrong is a classic ERP defect: two users click
"Create" at the same moment and both receive number 42.

The counter lives in a dedicated table and is claimed with ``UPDATE ...
RETURNING`` under a row lock, inside the caller's transaction. Concurrent
requests therefore serialise on that single row rather than racing on
``SELECT MAX(number) + 1``.

A PostgreSQL sequence would be faster but is transaction-independent: a rolled
back document would burn a number and leave a permanent gap in a legally
sequential series, which is not acceptable for GST-relevant documents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Integer, String, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from src.core.clock import today
from src.core.models import TenantEntity


class DocumentSequence(TenantEntity):
    """One counter per (company, document type, period key)."""

    __tablename__ = "document_sequences"

    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    period_key: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("''"))
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "doc_type",
            "period_key",
            name="uq_document_sequences_company_id_doc_type_period_key",
        ),
    )


@dataclass(frozen=True, slots=True)
class DocType:
    """Numbering policy for one document type."""

    key: str
    prefix: str
    #: Reset the counter each financial year. Statutory documents must; internal
    #: ones such as stock transfers need not.
    yearly: bool = True
    padding: int = 5


class DocTypes:
    SALES_ORDER = DocType("sales_order", "SO")
    SALES_INVOICE = DocType("sales_invoice", "INV")
    PURCHASE_REQUISITION = DocType("purchase_requisition", "PR")
    RFQ = DocType("rfq", "RFQ")
    PURCHASE_ORDER = DocType("purchase_order", "PO")
    GRN = DocType("grn", "GRN")
    PURCHASE_RETURN = DocType("purchase_return", "PRTN")
    PRODUCTION_ORDER = DocType("production_order", "MO")
    WORK_ORDER = DocType("work_order", "WO")
    ASSEMBLY_ORDER = DocType("assembly_order", "AO")
    JOB_WORK_ORDER = DocType("job_work_order", "JW")
    JOB_WORK_CHALLAN = DocType("job_work_challan", "JWC")
    MATERIAL_ISSUE = DocType("material_issue", "MI")
    MATERIAL_RETURN = DocType("material_return", "MR")
    STOCK_TRANSFER = DocType("stock_transfer", "ST", yearly=False)
    STOCK_ADJUSTMENT = DocType("stock_adjustment", "ADJ", yearly=False)
    PHYSICAL_COUNT = DocType("physical_count", "PC", yearly=False)
    QC_INSPECTION = DocType("qc_inspection", "QC")
    PACKING_ORDER = DocType("packing_order", "PK")
    DISPATCH = DocType("dispatch", "DSP")
    VENDOR_INVOICE = DocType("vendor_invoice", "VINV")
    PAYMENT = DocType("payment", "PAY")
    MRP_RUN = DocType("mrp_run", "MRP", yearly=False)
    BOM = DocType("bom", "BOM", yearly=False)


def financial_year_key(on: date | None = None, start_month: int = 4) -> str:
    """Indian financial year label: 1 Apr 2026 - 31 Mar 2027 becomes ``2026-27``."""
    when = on or today()
    start_year = when.year if when.month >= start_month else when.year - 1
    return f"{start_year}-{str((start_year + 1) % 100).zfill(2)}"


async def next_document_number(
    session: AsyncSession,
    company_id: uuid.UUID,
    doc_type: DocType,
    *,
    on: date | None = None,
    fy_start_month: int = 4,
) -> str:
    """Claim the next number for ``doc_type`` and return the formatted code.

    Must be called inside the same transaction that persists the document: if
    that transaction rolls back, the number is released rather than skipped.

    Uses ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` so the first document
    of a new period and every subsequent one take the identical code path, with
    no read-then-write window for a concurrent caller to slip through.
    """
    period = financial_year_key(on, fy_start_month) if doc_type.yearly else ""

    result = await session.execute(
        text(
            """
            INSERT INTO document_sequences
                (id, company_id, doc_type, period_key, prefix, last_number, padding,
                 created_at, updated_at)
            VALUES
                (:id, :company_id, :doc_type, :period_key, :prefix, 1, :padding,
                 now(), now())
            ON CONFLICT (company_id, doc_type, period_key)
            DO UPDATE SET last_number = document_sequences.last_number + 1,
                          updated_at  = now()
            RETURNING last_number
            """
        ),
        {
            "id": uuid.uuid4(),
            "company_id": company_id,
            "doc_type": doc_type.key,
            "period_key": period,
            "prefix": doc_type.prefix,
            "padding": doc_type.padding,
        },
    )
    number = int(result.scalar_one())

    body = str(number).zfill(doc_type.padding)
    return f"{doc_type.prefix}/{period}/{body}" if period else f"{doc_type.prefix}/{body}"


def normalise_code(value: str) -> str:
    """Canonical form for user-entered master-data codes.

    Upper-cased and internally-spaced-collapsed so ``rm 001``, ``RM-001`` and
    ``rm001`` cannot become three different items.
    """
    return " ".join(value.strip().upper().split())


__all__ = [
    "DocType",
    "DocTypes",
    "DocumentSequence",
    "financial_year_key",
    "next_document_number",
    "normalise_code",
]
