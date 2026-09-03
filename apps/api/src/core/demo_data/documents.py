"""Stage 6: files attached to the documents earlier stages created.

The document centre was the one screen the other five stages left empty, and an
empty document list is the least informative thing this tool can produce: the
screen's whole job is filtering by the record a file belongs to, which cannot be
demonstrated by nothing.

These are real uploads. :meth:`DocumentService.upload` sniffs the leading bytes
against the declared MIME type and refuses a mismatch, so the payloads below are
genuinely well-formed files rather than text with a hopeful extension - a
one-page PDF built by hand, and CSV where that is the honest shape. That also
means this stage exercises the validation path, which a direct INSERT would skip
entirely: `text/plain` is NOT an accepted type here, which is a reasonable rule
for an ERP (a .txt attachment on a purchase order is nearly always a mistake) and
one this file found by being refused.

Each file is filed against a record from an earlier stage through
``entity_type``/``entity_id``. The schema refuses half a reference - "the file is
filed against something no query can express, so it is invisible for ever" - so
a file whose target is missing is skipped rather than uploaded unattached.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from io import BytesIO

from fastapi import UploadFile
from sqlalchemy import select

from src.core.demo_data import DemoContext
from src.core.logging import get_logger
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.schemas import DocumentUploadRequest
from src.modules.documents.service import DocumentService

log = get_logger(__name__)

STAGE = "documents"


def _skip(ctx: DemoContext, reason: str) -> None:
    ctx.skipped.append((STAGE, reason))
    log.info("demo.documents_skipped", reason=reason)


# --- file bodies -------------------------------------------------------------
# Small, valid, and deterministic. `assert_content_matches` reads the first bytes
# and compares them against the declared type, so a PDF has to actually begin
# with %PDF and carry a usable trailer.


def _pdf(title: str, body: str) -> bytes:
    """A minimal one-page PDF that a viewer will actually open.

    Hand-built rather than pulled from a library: this is the only place the
    project would need a PDF writer, and a dependency added for demonstration
    data is a dependency in production too.
    """
    text = f"BT /F1 14 Tf 60 760 Td ({title}) Tj 0 -22 Td /F1 10 Tf ({body}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, source in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{source}\nendobj\n".encode("latin-1")

    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


@dataclass(frozen=True, slots=True)
class _FileSpec:
    """One file, and the record it belongs to."""

    #: Stable part of the stored name, so a re-run recognises its own upload.
    key: str
    file_name: str
    mime_type: str
    category: DocumentCategory
    #: ``ctx.refs`` kind and code of the record this is filed against.
    entity_kind: str
    entity_code: str
    #: What ``entity_type`` the documents module knows this record as.
    entity_type: str
    payload: bytes


def _specs(ctx: DemoContext) -> tuple[_FileSpec, ...]:
    return (
        _FileSpec(
            key="PO-COPY",
            file_name="purchase-order-brass-rod.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.ATTACHMENT,
            entity_kind="purchase_order",
            entity_code=ctx.code("PO", "BRASS"),
            entity_type="purchase_order",
            payload=_pdf(
                "Purchase Order - Brass Rod 12mm",
                "Signed copy returned by the supplier.",
            ),
        ),
        _FileSpec(
            key="GRN-CHALLAN",
            file_name="delivery-challan.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.ATTACHMENT,
            entity_kind="goods_receipt",
            entity_code=ctx.code("GRN", "METALS"),
            entity_type="goods_receipt",
            payload=_pdf(
                "Delivery Challan",
                "Carrier copy, signed at the gate on receipt.",
            ),
        ),
        _FileSpec(
            key="QC-CERT",
            file_name="mill-test-certificate.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.DOCUMENT,
            entity_kind="item",
            entity_code=ctx.code("RM", "BRASS"),
            entity_type="item",
            payload=_pdf(
                "Mill Test Certificate - IS 319 Brass",
                "Chemical composition and hardness for the supplied heat.",
            ),
        ),
        _FileSpec(
            key="VENDOR-MSME",
            file_name="udyam-registration.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.DOCUMENT,
            entity_kind="vendor",
            entity_code=ctx.code("VEN", "BRASS"),
            entity_type="vendor",
            payload=_pdf(
                "Udyam Registration Certificate",
                "MSME registration supporting the vendor master record.",
            ),
        ),
        _FileSpec(
            key="SO-PO-COPY",
            file_name="customer-purchase-order.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.ATTACHMENT,
            entity_kind="sales_order",
            entity_code=ctx.code("SO", "CONFIRMED"),
            entity_type="sales_order",
            payload=_pdf(
                "Customer Purchase Order",
                "The order this sales order was raised against.",
            ),
        ),
        _FileSpec(
            key="STOCK-EXTRACT",
            file_name="opening-stock.csv",
            mime_type="text/csv",
            category=DocumentCategory.EXPORT,
            entity_kind="warehouse",
            entity_code="WH-RM",
            entity_type="warehouse",
            payload=(
                b"item_code,description,uom,quantity\n"
                b"DM-RM-BRASS,Brass Rod 12mm,KG,500.0000\n"
                b"DM-RM-SS,Stainless Rod 10mm,KG,300.0000\n"
                b"DM-BO-SCREW-CSK,Countersunk Screw M4x20,NOS,5000.0000\n"
            ),
        ),
        _FileSpec(
            key="PACKING-NOTE",
            file_name="packing-instructions.pdf",
            mime_type="application/pdf",
            category=DocumentCategory.DOCUMENT,
            entity_kind="item",
            entity_code=ctx.code("FG", "LEVER-AB"),
            entity_type="item",
            payload=_pdf(
                "Packing Instructions - Antique Brass Lever Set",
                "Acid-free tissue per lever; 20 inner boxes per carton; max 4 high.",
            ),
        ),
    )


async def _already_uploaded(ctx: DemoContext, file_name: str, entity_id: uuid.UUID) -> bool:
    """Has this exact file already been filed against this exact record?

    Documents carry no code column, so the natural key is the pair the screen
    itself shows - the file's name and the record it hangs off. Matching on name
    alone would skip a challan on a second receipt that legitimately shares it.

    Document is a plain TenantEntity with no soft-delete column - a
    deleted file is gone, because the bytes behind it are - so there is no
    deleted_at to filter on here.
    """
    stmt = (
        select(Document.id)
        .where(
            Document.company_id == ctx.company_id,
            Document.file_name == file_name,
            Document.entity_id == entity_id,
        )
        .limit(1)
    )
    return (await ctx.session.execute(stmt)).first() is not None


async def seed(ctx: DemoContext) -> None:
    service = DocumentService(ctx.session, ctx.tenant)
    created = existing = 0

    for spec in _specs(ctx):
        entity_id = ctx.maybe_id(spec.entity_kind, spec.entity_code)
        if entity_id is None:
            # Half a reference is refused by the schema, and rightly - a file
            # filed against nothing is invisible to every query the screen makes.
            _skip(
                ctx,
                f"{spec.file_name} not uploaded: no {spec.entity_kind} registered as "
                f"{spec.entity_code}, and a document filed against nothing cannot be found again",
            )
            continue

        if await _already_uploaded(ctx, spec.file_name, entity_id):
            existing += 1
            ctx.remember(STAGE, spec.key, entity_id)
            continue

        upload = UploadFile(
            file=BytesIO(spec.payload),
            filename=spec.file_name,
            headers={"content-type": spec.mime_type},  # type: ignore[arg-type]
        )
        document = await service.upload(
            upload,
            DocumentUploadRequest(
                category=spec.category,
                entity_type=spec.entity_type,
                entity_id=entity_id,
                is_public=False,
            ),
        )
        ctx.remember("document", spec.key, document.id)
        created += 1

    ctx.note(STAGE, created=created, existing=existing)
