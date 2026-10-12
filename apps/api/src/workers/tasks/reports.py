"""Background generation of report artefacts (master rules sections 22, 35, 43).

A packing list for a fifty-carton shipment is several pages of tables. Rendering
it costs hundreds of milliseconds of CPU and a spike of memory, and doing that on
an HTTP worker means one person clicking "print" makes the API slower for
everybody. So the endpoint queues this task and returns ``202`` with a job id,
and the file appears in the document store when it is ready.

Three properties this task has to hold, and how each is achieved:

**The bytes and the row commit together, or neither does.** The PDF is written to
disk first because the ``documents`` row has to record its size and checksum, and
those are only known once it is written. If the row then fails to commit, the
file is unlinked in the ``except`` - nothing would ever reference it again, and a
storage tree full of files no row points at is unauditable.

**Nothing blocking runs on the event loop.** Rendering and the file write are
plain synchronous functions handed to ``anyio.to_thread.run_sync``, the same way
``documents.service`` handles its own I/O.

**Re-delivery is safe.** ``task_acks_late`` is on, so a worker killed mid-render
has its task delivered again. The storage key ends in a fresh UUIDv7 and the file
is opened exclusively, so the second delivery writes a *new* artefact rather than
colliding with, or half-overwriting, the first. That leaves at most a duplicate
document - visible, harmless and expiring on its own retention - rather than a
corrupt one.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import anyio
from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.core.clock import utcnow
from src.core.database import session_scope, transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, InvalidStateTransitionError
from src.core.ids import new_id
from src.core.logging import get_logger
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.documents.models import Document, DocumentCategory
from src.modules.documents.repository import DocumentRepository
from src.modules.documents.service import (
    KNOWN_TYPES,
    build_storage_key,
    default_expiry,
    resolve_storage_path,
)
from src.modules.finance.repository import InventoryValuationRepository
from src.modules.finance.schemas import ValuationFilters
from src.modules.finance.service import valuation_row
from src.modules.notifications.models import NotificationSeverity, NotificationType
from src.modules.notifications.service import notify
from src.modules.packing import service as packing_service
from src.modules.reports import service as reports_service
from src.workers.celery_app import async_task

log = get_logger("worker.reports")

PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: What the generated document is filed against, so "every artefact for this
#: packing order" is one indexed lookup on ``documents``.
PACKING_LIST_ENTITY = "packing_order"

#: Page geometry. 15 mm margins keep the table inside the printable area of every
#: office printer we have met, without wasting a column of width.
PAGE_MARGIN = 15 * mm

#: Cartons per rendered page. A packing list is read carton by carton, so the
#: page break goes between cartons rather than wherever the table happens to
#: overflow - a carton split across a page boundary is exactly the document a
#: goods-inwards clerk mis-reads.
CARTONS_PER_PAGE = 6

#: Rows fetched from the valuation aggregate per round trip. Large enough that a
#: four-thousand-position company is eight queries, small enough that no single
#: result set is held in the worker at an unbounded size.
VALUATION_CHUNK_ROWS = 500

#: Hard ceiling on one valuation export. A company whose valuation exceeds this
#: has outgrown a spreadsheet, and an unbounded export would be an unbounded
#: result set wearing a background job's clothes (section 31). The workbook says
#: so on its face rather than silently stopping.
MAX_VALUATION_EXPORT_ROWS = 100_000

#: What a valuation workbook is filed against. The run is company-wide, so it
#: belongs to no single entity - ``documents`` requires entity type and id to be
#: both present or both absent, and both are absent here.
VALUATION_ENTITY = "inventory_valuation"


# -----------------------------------------------------------------------------
# Rendering - synchronous, run in a worker thread
# -----------------------------------------------------------------------------


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("PackingTitle", parent=base["Title"], fontSize=16, spaceAfter=4),
        "heading": ParagraphStyle(
            "PackingHeading", parent=base["Heading2"], fontSize=11, spaceBefore=8, spaceAfter=4
        ),
        "body": ParagraphStyle("PackingBody", parent=base["BodyText"], fontSize=8, leading=10),
        "meta": ParagraphStyle("PackingMeta", parent=base["BodyText"], fontSize=8, leading=11),
        "right": ParagraphStyle(
            "PackingRight", parent=base["BodyText"], fontSize=8, alignment=TA_RIGHT
        ),
    }


_TABLE_STYLE = TableStyle(
    [
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
)


def _header_block(data: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    rows = [
        ["Customer", data.get("customer_name") or "-", "Status", data.get("status") or "-"],
        [
            "Customer code",
            data.get("customer_code") or "-",
            "Sales order",
            data.get("sales_order_code") or "-",
        ],
        [
            "Planned date",
            data.get("planned_date") or "-",
            "Packed",
            data.get("packed_date") or "not yet",
        ],
        [
            "Cartons",
            str(data.get("total_cartons", 0)),
            "Gross / net (kg)",
            f"{data.get('total_gross_weight', '0')} / {data.get('total_net_weight', '0')}",
        ],
    ]
    table = Table(rows, colWidths=[30 * mm, 55 * mm, 30 * mm, 55 * mm])
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return [
        Paragraph(f"Packing list {data.get('code', '')}", styles["title"]),
        table,
    ]


def _line_table(data: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    header = ["#", "Item", "Description", "To pack", "Packed"]
    rows: list[list[Any]] = [header]
    for line in data.get("lines", []):
        rows.append(
            [
                str(line["line_number"]),
                Paragraph(line["item_code"], styles["body"]),
                Paragraph(line["item_name"], styles["body"]),
                line["quantity_to_pack"],
                line["packed_quantity"],
            ]
        )
    if len(rows) == 1:
        rows.append(["-", "", Paragraph("No lines", styles["body"]), "", ""])

    table = Table(rows, colWidths=[10 * mm, 35 * mm, 75 * mm, 25 * mm, 25 * mm], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return [Paragraph("Ordered", styles["heading"]), table]


def _carton_block(carton: dict[str, Any], styles: dict[str, ParagraphStyle]) -> list[Any]:
    """One carton's heading and content table, kept on a single page."""
    heading = Paragraph(
        f"Carton {carton['carton_number']} &mdash; {carton['barcode']} "
        f"({carton['carton_type']}, {carton['gross_weight_kg']} kg gross / "
        f"{carton['net_weight_kg']} kg net"
        f"{'' if carton['is_sealed'] else ', NOT SEALED'})",
        styles["heading"],
    )

    rows: list[list[Any]] = [["Item", "Description", "Qty", "Batch", "Serials"]]
    for content in carton.get("contents", []):
        serials = content.get("serial_numbers") or []
        rows.append(
            [
                Paragraph(content["item_code"], styles["body"]),
                Paragraph(content["item_name"], styles["body"]),
                content["quantity"],
                content.get("batch_number") or "-",
                # The serial values themselves are on the traceability record;
                # printing several thousand of them would make the packing list
                # unusable as a packing list.
                str(len(serials)) if serials else "-",
            ]
        )
    if len(rows) == 1:
        rows.append([Paragraph("Empty carton", styles["body"]), "", "", "", ""])

    table = Table(rows, colWidths=[30 * mm, 70 * mm, 22 * mm, 30 * mm, 18 * mm], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    return [KeepTogether([heading, table])]


def render_packing_list_pdf(data: dict[str, Any]) -> bytes:
    """Render one packing list to PDF bytes.

    Pure and synchronous: it takes the plain mapping
    :func:`~src.modules.packing.service.packing_list_data` produced and returns
    bytes. No session, no ORM instance and no I/O, which is what lets it run on a
    worker thread and be exercised without a database.
    """
    styles = _styles()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title=f"Packing list {data.get('code', '')}",
    )

    story: list[Any] = []
    story.extend(_header_block(data, styles))
    story.append(Spacer(1, 6))
    story.extend(_line_table(data, styles))

    cartons = data.get("cartons", [])
    for index, carton in enumerate(cartons):
        if index and index % CARTONS_PER_PAGE == 0:
            story.append(PageBreak())
        story.extend(_carton_block(carton, styles))

    if data.get("notes"):
        story.append(Spacer(1, 8))
        story.append(Paragraph("Notes", styles["heading"]))
        story.append(Paragraph(str(data["notes"]), styles["body"]))

    story.append(Spacer(1, 10))
    story.append(
        Paragraph(f"Generated {utcnow().isoformat(timespec='seconds')} UTC", styles["right"])
    )

    document.build(story)
    return buffer.getvalue()


#: The ``documents.entity_type`` a PO PDF is filed under - the purchase order
#: itself, so the vendor portal and the PO's file list both find it.
PURCHASE_ORDER_ENTITY = "purchase_order"

#: Two decimals, rounded half-up the way a printed rupee amount is.
_PAISE = Decimal("0.01")


def indian_money(value: Any) -> str:
    """An amount with Indian digit grouping: ``1,23,45,678.90``.

    Local to this module for now; WP A2 adds money helpers in the commercial
    lane, and the integrator consolidates the two.
    """
    amount = Decimal(str(value or 0)).quantize(_PAISE, rounding=ROUND_HALF_UP)
    integer, fraction = f"{amount:.2f}".split(".")
    sign = "-" if integer.startswith("-") else ""
    digits = integer.lstrip("-")
    tail = digits[-3:]
    head = digits[:-3]
    groups: list[str] = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return sign + ",".join([*groups, tail]) + "." + fraction


def plain_quantity(value: Any) -> str:
    """A quantity as a document prints it: ``20``, ``12.5`` - not ``20.0000``."""
    text = format(Decimal(str(value or 0)).normalize(), "f")
    return "0" if text == "-0" else text


#: The address lines of a party block, in reading order.
_ADDRESS_KEYS: tuple[str, ...] = ("address_line1", "address_line2", "city", "state", "postal_code")

#: The order totals as the PDF labels them.
_PO_TOTALS: tuple[tuple[str, str], ...] = (
    ("subtotal", "Subtotal"),
    ("discount", "Discount"),
    ("tax_amount", "Tax"),
    ("freight_amount", "Freight"),
    ("total_amount", "Total"),
)


def render_purchase_order_pdf(data: dict[str, Any]) -> bytes:
    """Render a purchase order to PDF bytes. Pure: no database, no clock, no I/O.

    Every stored value is escaped before it becomes a reportlab ``Paragraph``,
    whose mini-markup would otherwise interpret a ``<`` in an item name.
    """
    from xml.sax.saxutils import escape

    styles = _styles()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title=f"Purchase order {data.get('code', '')}",
    )

    def paragraph(value: Any, style: str = "body") -> Paragraph:
        return Paragraph(escape(str(value or "")), styles[style])

    def party(label: str, block: dict[str, Any]) -> list[Any]:
        address = ", ".join(str(block[key]) for key in _ADDRESS_KEYS if block.get(key))
        return [
            paragraph(f"{label}: {block.get('name') or ''}", "meta"),
            *([paragraph(address)] if address else []),
            *([paragraph(f"GSTIN: {block['gstin']}")] if block.get("gstin") else []),
        ]

    currency = data.get("currency") or "INR"
    story: list[Any] = [
        Paragraph("PURCHASE ORDER", styles["title"]),
        *party("Buyer", data.get("company") or {}),
        Spacer(1, 4),
        *party("Vendor", data.get("vendor") or {}),
        Spacer(1, 8),
        paragraph(f"PO number: {data.get('code') or ''}", "meta"),
        paragraph(f"PO date: {data.get('po_date') or ''}    Status: {data.get('status') or ''}"),
        paragraph(f"Expected delivery: {data.get('expected_delivery_date') or 'not set'}"),
        *(
            [paragraph(f"Vendor committed delivery: {data['vendor_committed_date']}")]
            if data.get("vendor_committed_date")
            else []
        ),
        paragraph(f"Delivery address: {data.get('delivery_address') or ''}"),
        Spacer(1, 8),
    ]
    rows: list[list[Any]] = [
        [
            paragraph(header)
            for header in (
                "Item / vendor code",
                "Description / HSN",
                "Qty / UoM",
                "Rate",
                "Discount",
                "Tax",
                "Total",
            )
        ]
    ]
    for line in data.get("lines", []):
        rows.append(
            [
                paragraph(
                    " / ".join(
                        str(v) for v in (line.get("code"), line.get("vendor_item_code")) if v
                    )
                ),
                paragraph(
                    str(line.get("description") or "")
                    + (f" (HSN {line['hsn']})" if line.get("hsn") else "")
                ),
                paragraph(f"{plain_quantity(line.get('quantity'))} {line.get('uom') or ''}"),
                *[
                    paragraph(indian_money(line.get(key)), "right")
                    for key in ("unit_price", "discount", "tax", "total")
                ],
            ]
        )
    table = Table(rows, colWidths=[70, 125, 50, 60, 50, 50, 65], repeatRows=1)
    table.setStyle(_TABLE_STYLE)
    story.append(table)
    story.append(Spacer(1, 6))
    for key, label in _PO_TOTALS:
        story.append(paragraph(f"{label}: {currency} {indian_money(data.get(key))}", "right"))
    if data.get("terms"):
        story.extend([Spacer(1, 10), paragraph("Terms", "meta"), paragraph(data.get("terms"))])
    story.extend([Spacer(1, 28), paragraph("Authorised signatory", "meta")])
    document.build(story)
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# Filesystem primitives - blocking, handed to a worker thread
# -----------------------------------------------------------------------------


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # "xb" - exclusive create. The key ends in a fresh UUIDv7, so an existing
    # file at that path means something is badly wrong and must not be silently
    # overwritten. Same reasoning as documents.service.
    with path.open("xb") as handle:
        handle.write(payload)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _safe_file_name(code: str) -> str:
    """A display file name derived from a document number.

    Only the display name - the path is built by ``build_storage_key`` from
    server-side components alone, so nothing here can influence where the file
    lands. Separators are still replaced, because this string ends up in a
    ``Content-Disposition`` header.
    """
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in code).strip("-")
    return f"packing-list-{cleaned or 'document'}.pdf"


# -----------------------------------------------------------------------------
# The task
# -----------------------------------------------------------------------------


@async_task(name="reports.generate_packing_list", queue="reports")
async def generate_packing_list(
    packing_order_id: str,
    snapshot_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Render one packing order's packing list and file it in the document store.

    The job row (``snapshot_id``) is driven through the same lifecycle as every
    other background report: claimed on entry, completed with the document id
    once the row is written, and failed on a fresh session if anything raises -
    see :func:`generate_report` for why each of those matters. Without it the
    client polls a job that stays QUEUED for ever.

    Arguments are plain strings because Celery serialises task payloads as JSON;
    ``request_id`` is not used here but is read by the ``task_prerun`` signal so
    the API's correlation id follows the work into the worker's logs (section 55).
    """
    tenant = TenantContext(company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id))
    order_id = uuid.UUID(packing_order_id)
    identifier = uuid.UUID(snapshot_id)
    moment = utcnow()

    try:
        async with session_scope() as session:
            try:
                snapshot = await reports_service.start_snapshot(session, tenant, identifier)
            except InvalidStateTransitionError:
                log.warning("reports.packing_list_not_queued", snapshot_id=snapshot_id)
                return {
                    "snapshot_id": snapshot_id,
                    "status": "SKIPPED",
                    "reason": "the job is no longer queued",
                }

            data = await packing_service.packing_list_data(session, tenant, order_id)

            payload = await anyio.to_thread.run_sync(render_packing_list_pdf, data)
            checksum = hashlib.sha256(payload).hexdigest()

            rule = KNOWN_TYPES[PDF_MIME_TYPE]
            storage_key = build_storage_key(
                category=DocumentCategory.REPORT,
                company_id=tenant.company_id,
                rule=rule,
                moment=moment,
                file_id=new_id(),
            )
            destination = resolve_storage_path(storage_key)
            await anyio.to_thread.run_sync(_write_bytes, destination, payload)

            try:
                document = Document(
                    file_name=_safe_file_name(str(data.get("code", ""))),
                    storage_key=storage_key,
                    mime_type=PDF_MIME_TYPE,
                    size_bytes=len(payload),
                    checksum_sha256=checksum,
                    category=DocumentCategory.REPORT,
                    entity_type=PACKING_LIST_ENTITY,
                    entity_id=order_id,
                    uploaded_by_id=tenant.user_id,
                    is_public=False,
                    expires_at=default_expiry(DocumentCategory.REPORT, moment),
                )
                DocumentRepository(session, tenant).add(document)
                await session.flush()

                await record_audit(
                    session,
                    company_id=tenant.company_id,
                    action=AuditAction.DATA_EXPORT,
                    entity_type=PACKING_LIST_ENTITY,
                    entity_id=order_id,
                    entity_code=str(data.get("code", "")),
                    summary=(
                        f"Generated the packing list for {data.get('code', '')} "
                        f"({len(payload)} bytes)"
                    ),
                    user_id=tenant.user_id,
                    new_values={
                        "document_id": document.id,
                        "file_name": document.file_name,
                        "size_bytes": document.size_bytes,
                        "checksum_sha256": checksum,
                        "carton_count": len(data.get("cartons", [])),
                    },
                )

                await reports_service.complete_snapshot(
                    session,
                    tenant,
                    snapshot_id=snapshot.id,
                    row_count=len(data.get("cartons", [])),
                    document_id=document.id,
                )
            except Exception:
                # The row never committed, so nothing will ever reference these
                # bytes. Removing them here is the only cheap chance to do so.
                await anyio.to_thread.run_sync(_unlink, destination)
                raise

            document_id = str(document.id)
            size_bytes = document.size_bytes

    except Exception as exc:
        # A separate session: the one the failure happened on cannot be used to
        # write anything, so the job would otherwise stay RUNNING for ever.
        async with session_scope() as session:
            await reports_service.fail_snapshot(
                session, tenant, snapshot_id=identifier, error=_failure_message(exc)
            )
        log.error(
            "reports.packing_list_failed", snapshot_id=snapshot_id, error=str(exc), exc_info=exc
        )
        raise

    log.info(
        "reports.packing_list_generated",
        snapshot_id=snapshot_id,
        packing_order_id=packing_order_id,
        document_id=document_id,
        size_bytes=size_bytes,
    )
    return {
        "snapshot_id": snapshot_id,
        "status": "COMPLETED",
        "packing_order_id": packing_order_id,
        "packing_order_code": str(data.get("code", "")),
        "document_id": document_id,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
        "carton_count": len(data.get("cartons", [])),
    }


# -----------------------------------------------------------------------------
# Inventory valuation - the full-company run (master rules sections 22, 25, 35)
# -----------------------------------------------------------------------------

#: Column headings, and the width each needs to be readable without the reader
#: dragging borders around.
VALUATION_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Item code", 18),
    ("Item", 40),
    ("Warehouse code", 16),
    ("Warehouse", 28),
    ("On hand", 14),
    ("Average cost", 16),
    ("Value", 18),
    ("Recorded value", 18),
    ("Difference", 14),
)


def render_inventory_valuation_xlsx(rows: list[dict[str, Any]], meta: dict[str, Any]) -> bytes:
    """Render a valuation to XLSX bytes.

    Pure and synchronous: it takes plain mappings and returns bytes, with no
    session, no ORM instance and no I/O - which is what lets it run on a worker
    thread and be exercised without a database.

    Every number is handed to openpyxl as a ``Decimal``. The XLSX format itself
    stores numbers as doubles, so the workbook is where exactness ends - but
    converting to ``float`` in *our* code on the way there would round twice,
    once for us and once for the format, and the first of those roundings is the
    one master rules section 84 exists to keep out. Nothing in this module
    constructs a float.

    ``Difference`` is the value the inventory engine recorded on the balances
    less the same total recomputed from ``inventory_cost_layers`` itself. It is
    a real cross-check rather than a restatement: nothing the balance row says
    about itself appears on the second side, so a position whose ``stock_value``
    has come apart from the layers behind it shows up here. A few paise is the
    four-place rounding ``stock_value`` carries per position; anything larger is
    a reconciliation item. Putting it on the report is how anyone finds out
    which.
    """
    workbook = Workbook(write_only=False)
    sheet = workbook.active
    sheet.title = "Valuation"
    sheet.freeze_panes = "A2"

    for index, (heading, width) in enumerate(VALUATION_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index, value=heading)
        cell.font = Font(bold=True)
        sheet.column_dimensions[cell.column_letter].width = width

    for offset, row in enumerate(rows, start=2):
        value = Decimal(str(row["value"]))
        recorded = Decimal(str(row["recorded_value"]))
        # Through `_neutralise_formula`, exactly as the generic renderer does:
        # these four are the master-data strings a user controls, and an item
        # name beginning with `=` is a live formula in the opened workbook.
        sheet.cell(row=offset, column=1, value=_neutralise_formula(str(row["item_code"])))
        sheet.cell(row=offset, column=2, value=_neutralise_formula(str(row["item_name"])))
        sheet.cell(row=offset, column=3, value=_neutralise_formula(str(row["warehouse_code"])))
        sheet.cell(row=offset, column=4, value=_neutralise_formula(str(row["warehouse_name"])))
        sheet.cell(row=offset, column=5, value=Decimal(str(row["on_hand_quantity"])))
        sheet.cell(row=offset, column=6, value=Decimal(str(row["average_cost"])))
        sheet.cell(row=offset, column=7, value=value)
        sheet.cell(row=offset, column=8, value=recorded)
        sheet.cell(row=offset, column=9, value=value - recorded)

    summary = workbook.create_sheet("Summary")
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 28
    for index, (label, cell_value) in enumerate(
        (
            ("Generated (UTC)", meta.get("generated_at", "")),
            ("Positions valued", len(rows)),
            ("Positions in company", meta.get("row_count", len(rows))),
            ("Total quantity", Decimal(str(meta.get("total_quantity", "0")))),
            ("Total value", Decimal(str(meta.get("total_value", "0")))),
            ("Warehouse filter", meta.get("warehouse_id") or "all"),
            ("Category filter", meta.get("category_id") or "all"),
            ("Truncated", "yes" if meta.get("truncated") else "no"),
        ),
        start=1,
    ):
        summary.cell(row=index, column=1, value=label).font = Font(bold=True)
        summary.cell(row=index, column=2, value=cell_value)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _valuation_file_name(moment: Any) -> str:
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    return f"inventory-valuation-{stamp}.xlsx"


@async_task(name="reports.inventory_valuation", queue="reports")
async def inventory_valuation(
    company_id: str,
    user_id: str,
    snapshot_id: str,
    warehouse_id: str | None = None,
    category_id: str | None = None,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Value the whole company's on-hand stock and file the workbook.

    The paginated endpoint answers "what is this warehouse worth?"; this answers
    "what is everything worth?", which on a real item master is thousands of
    positions and is not something an HTTP worker holds a connection open for
    (sections 22, 35).

    The aggregate is walked in fixed-size chunks over a stable ``(item_id,
    warehouse_id)`` ordering. Ordering by value instead - which is right for the
    screen - would tie between equal-valued positions and let a row appear in
    two chunks or in none.

    The job row (``snapshot_id``) is claimed on entry, completed with the
    document id once the row is written, and failed on a fresh session if
    anything raises - see :func:`generate_report` for why each of those matters.
    """
    tenant = TenantContext(company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id))
    filters = ValuationFilters(
        warehouse_id=uuid.UUID(warehouse_id) if warehouse_id else None,
        category_id=uuid.UUID(category_id) if category_id else None,
    )
    identifier = uuid.UUID(snapshot_id)
    moment = utcnow()

    try:
        async with session_scope() as session:
            try:
                snapshot = await reports_service.start_snapshot(session, tenant, identifier)
            except InvalidStateTransitionError:
                log.warning("reports.inventory_valuation_not_queued", snapshot_id=snapshot_id)
                return {
                    "snapshot_id": snapshot_id,
                    "status": "SKIPPED",
                    "reason": "the job is no longer queued",
                }

            repo = InventoryValuationRepository(session, tenant)
            row_count, total_quantity, total_value = await repo.valuation_totals(filters)

            stmt = repo.valuation_export_stmt(filters)
            rows: list[dict[str, Any]] = []
            offset = 0
            while len(rows) < MAX_VALUATION_EXPORT_ROWS:
                chunk = (
                    await session.execute(stmt.limit(VALUATION_CHUNK_ROWS).offset(offset))
                ).all()
                if not chunk:
                    break
                rows.extend(valuation_row(row).model_dump() for row in chunk)
                offset += VALUATION_CHUNK_ROWS

            truncated = len(rows) < row_count
            payload = await anyio.to_thread.run_sync(
                render_inventory_valuation_xlsx,
                rows,
                {
                    "generated_at": moment.isoformat(timespec="seconds"),
                    "row_count": row_count,
                    "total_quantity": total_quantity,
                    "total_value": total_value,
                    "warehouse_id": warehouse_id,
                    "category_id": category_id,
                    "truncated": truncated,
                },
            )
            checksum = hashlib.sha256(payload).hexdigest()

            rule = KNOWN_TYPES[XLSX_MIME_TYPE]
            storage_key = build_storage_key(
                category=DocumentCategory.REPORT,
                company_id=tenant.company_id,
                rule=rule,
                moment=moment,
                file_id=new_id(),
            )
            destination = resolve_storage_path(storage_key)
            await anyio.to_thread.run_sync(_write_bytes, destination, payload)

            try:
                document = Document(
                    file_name=_valuation_file_name(moment),
                    storage_key=storage_key,
                    mime_type=XLSX_MIME_TYPE,
                    size_bytes=len(payload),
                    checksum_sha256=checksum,
                    category=DocumentCategory.REPORT,
                    uploaded_by_id=tenant.user_id,
                    is_public=False,
                    expires_at=default_expiry(DocumentCategory.REPORT, moment),
                )
                DocumentRepository(session, tenant).add(document)
                await session.flush()

                await record_audit(
                    session,
                    company_id=tenant.company_id,
                    action=AuditAction.DATA_EXPORT,
                    entity_type=VALUATION_ENTITY,
                    summary=(
                        f"Valued {len(rows)} stock positions at {total_value} "
                        f"({len(payload)} bytes)"
                    ),
                    user_id=tenant.user_id,
                    new_values={
                        "document_id": document.id,
                        "file_name": document.file_name,
                        "size_bytes": document.size_bytes,
                        "checksum_sha256": checksum,
                        "row_count": row_count,
                        "rows_exported": len(rows),
                        "total_value": total_value,
                        "truncated": truncated,
                    },
                )

                await reports_service.complete_snapshot(
                    session,
                    tenant,
                    snapshot_id=snapshot.id,
                    row_count=len(rows),
                    document_id=document.id,
                )
            except Exception:
                # The row never committed, so nothing will ever reference these
                # bytes. Removing them here is the only cheap chance to do so.
                await anyio.to_thread.run_sync(_unlink, destination)
                raise

            document_id = str(document.id)
            size_bytes = document.size_bytes

    except Exception as exc:
        # A separate session: the one the failure happened on cannot be used to
        # write anything, so the job would otherwise stay RUNNING for ever.
        async with session_scope() as session:
            await reports_service.fail_snapshot(
                session, tenant, snapshot_id=identifier, error=_failure_message(exc)
            )
        log.error(
            "reports.inventory_valuation_failed",
            snapshot_id=snapshot_id,
            error=str(exc),
            exc_info=exc,
        )
        raise

    log.info(
        "reports.inventory_valuation_generated",
        snapshot_id=snapshot_id,
        company_id=company_id,
        document_id=document_id,
        rows=len(rows),
        truncated=truncated,
    )
    return {
        "snapshot_id": snapshot_id,
        "status": "COMPLETED",
        "document_id": document_id,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
        "row_count": row_count,
        "rows_exported": len(rows),
        "total_quantity": str(total_quantity),
        "total_value": str(total_value),
        "truncated": truncated,
    }


# -----------------------------------------------------------------------------
# Generic report generation (master rules section 25)
# -----------------------------------------------------------------------------

#: What a generated report artefact is filed against, so "the file for this job"
#: is one indexed lookup on ``documents`` rather than a guess from the filename.
GENERATED_REPORT_ENTITY = "report_snapshot"

#: Widest a generated column is allowed to be. Auto-sizing to the longest value
#: would let one 500-character note column push every useful column off the
#: screen; a reader can widen a narrow column, but cannot find one that has been
#: pushed out of view.
REPORT_COLUMN_WIDTH_LIMIT = 48
REPORT_COLUMN_WIDTH_MIN = 12

#: Characters Excel refuses in a sheet name. Report titles are free text, so they
#: are stripped rather than trusted.
_ILLEGAL_SHEET_CHARS = set("[]:*?/\\")


#: Characters that make Excel and LibreOffice treat a text cell as a formula.
#: ``@`` is included because it introduces a legacy function call, and the two
#: separator characters because a leading tab or carriage return is stripped by
#: the parser before the first significant character is examined.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _neutralise_formula(text: str) -> str:
    """Stop a stored text value being executed when the workbook is opened.

    An item name, vendor name or batch number beginning with ``=`` is written
    into the cell verbatim, and a spreadsheet reads it as a formula rather than
    as text - so ``=HYPERLINK("http://.../"&A1,"Click")`` in an item name
    becomes a live link in every export that surfaces it, and the DDE variants
    can prompt to run a command. The person opening the workbook is usually not
    the person who typed the value, and often has more authority than they do.

    Neutralised here rather than at input: ``=`` is a legitimate character in a
    part description, the API and the JSON export are unaffected by it, and
    rejecting it at the boundary would refuse honest data to fix a problem that
    only exists in one renderer. Prefixing a single quote is the conventional
    remedy - spreadsheets strip it on display and treat the rest as text.
    """
    return f"'{text}" if text.startswith(_FORMULA_LEADS) else text


def _cell_value(value: Any) -> Any:
    """Convert one report value into something openpyxl can store faithfully.

    Three of these conversions are not cosmetic. ``Decimal`` passes through
    untouched, because openpyxl stores it as an exact numeric cell and a
    ``float`` here would reintroduce binary rounding into the one artefact
    somebody reconciles against (section 84). Timezone-aware datetimes are
    converted to naive UTC, because XLSX has no timezone concept and openpyxl
    refuses an aware value outright - which would fail an entire export over a
    column nobody was reading. And text is checked for a formula lead: see
    :func:`_neutralise_formula`.
    """
    if isinstance(value, datetime):
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, str):
        return _neutralise_formula(value)
    if value is None or isinstance(value, (Decimal, bool, int, float, date)):
        return value
    return _neutralise_formula(str(value))


def render_report_xlsx(
    title: str,
    columns: list[dict[str, str]],
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
) -> bytes:
    """Render any report to XLSX bytes.

    Pure and synchronous - plain mappings in, bytes out, no session and no ORM -
    so it runs on a worker thread and is exercised in the unit tests without a
    database. The column list comes from the report's own row schema, so the
    spreadsheet cannot show a different set of columns from the API.

    The Summary sheet is not decoration: an artefact outlives the request that
    asked for it, so it has to be able to say what it is a report *of*, and a
    truncated export has to say so on its face rather than look like a complete
    answer that happens to be short.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "".join(c for c in title if c not in _ILLEGAL_SHEET_CHARS)[:31] or "Report"
    sheet.freeze_panes = "A2"

    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column["label"])
        cell.font = Font(bold=True)
        sheet.column_dimensions[cell.column_letter].width = max(
            REPORT_COLUMN_WIDTH_MIN,
            min(len(column["label"]) + 4, REPORT_COLUMN_WIDTH_LIMIT),
        )

    for offset, row in enumerate(rows, start=2):
        for index, column in enumerate(columns, start=1):
            sheet.cell(row=offset, column=index, value=_cell_value(row.get(column["name"])))

    summary = workbook.create_sheet("Summary")
    summary.column_dimensions["A"].width = 28
    summary.column_dimensions["B"].width = 44
    for index, (label, value) in enumerate(
        (
            ("Report", title),
            ("Generated (UTC)", meta.get("generated_at", "")),
            ("Rows", len(rows)),
            ("Period", meta.get("period") or "not applicable"),
            ("Filters", meta.get("filters") or "none"),
            ("Truncated", "yes" if meta.get("truncated") else "no"),
        ),
        start=1,
    ):
        summary.cell(row=index, column=1, value=label).font = Font(bold=True)
        summary.cell(row=index, column=2, value=_cell_value(value))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _report_file_name(report_key: str, moment: datetime) -> str:
    """A display file name. The storage path is built server-side, not from this."""
    slug = report_key.lower().replace("_", "-")
    return f"{slug}-{moment.strftime('%Y%m%d-%H%M%S')}.xlsx"


def _describe(parameters: dict[str, Any] | None) -> tuple[str, str]:
    """Human-readable period and filter lines for the Summary sheet."""
    parameters = parameters or {}
    period = parameters.get("period")
    period_text = f"{period['date_from']} to {period['date_to']}" if period else "not applicable"
    filters = {k: v for k, v in (parameters.get("filters") or {}).items() if v is not None}
    filter_text = ", ".join(f"{k}={v}" for k, v in sorted(filters.items())) or "none"
    return period_text, filter_text


def _failure_message(exc: BaseException) -> str:
    """A message safe to persist on the job row and show the person waiting.

    A deliberate :class:`AppError` carries text written for a user. Anything else
    is a bug, and a driver's exception string can contain SQL and row data, so
    only the type name is recorded and the traceback stays in the log (section 53).
    """
    if isinstance(exc, AppError):
        return exc.message
    return (
        f"The report failed with an unexpected {type(exc).__name__}. "
        "The full error is in the worker log against this task id."
    )


@async_task(name="reports.generate", queue="reports")
async def generate_report(
    snapshot_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Generate one queued report, file the workbook and tell the person waiting.

    Three properties this task has to hold.

    **Re-delivery is harmless.** ``task_acks_late`` is on, so a worker killed
    mid-render has its task delivered again. The job's state machine is what
    makes that survivable: the second delivery finds the snapshot no longer
    QUEUED and stops before it can write a second workbook for the same job. That
    is a deliberate refusal, not a crash, and it is logged as one.

    **A failure is recorded, not merely logged.** The run uses one session; if it
    fails, that session cannot be used again, so the job is marked FAILED on a
    fresh one. A job left RUNNING would be polled forever by a client that never
    learns it is not coming.

    **The bytes and the row commit together, or neither does.** The file is
    written first because the ``documents`` row records its size and checksum, and
    those are only known once it exists. If the row then fails, the file is
    unlinked - nothing would ever reference it again.

    ``request_id`` is not read here but is picked up by the ``task_prerun``
    signal, so the API's correlation id follows the work into the worker's logs.
    """
    tenant = TenantContext(company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id))
    identifier = uuid.UUID(snapshot_id)
    moment = utcnow()

    try:
        async with session_scope() as session:
            try:
                snapshot = await reports_service.start_snapshot(session, tenant, identifier)
            except InvalidStateTransitionError:
                log.warning("reports.generate_not_queued", snapshot_id=snapshot_id)
                return {
                    "snapshot_id": snapshot_id,
                    "status": "SKIPPED",
                    "reason": "the job is no longer queued",
                }

            definition, rows = await reports_service.snapshot_rows(session, tenant, snapshot)
            period_text, filter_text = _describe(snapshot.parameters)
            columns = [
                column.model_dump() for column in reports_service.columns_for(definition.row_model)
            ]
            payload = await anyio.to_thread.run_sync(
                render_report_xlsx,
                definition.title,
                columns,
                [row.model_dump() for row in rows],
                {
                    "generated_at": moment.isoformat(timespec="seconds"),
                    "period": period_text,
                    "filters": filter_text,
                    "truncated": len(rows) >= reports_service.REPORT_MAX_EXPORT_ROWS,
                },
            )
            checksum = hashlib.sha256(payload).hexdigest()

            rule = KNOWN_TYPES[XLSX_MIME_TYPE]
            storage_key = build_storage_key(
                category=DocumentCategory.REPORT,
                company_id=tenant.company_id,
                rule=rule,
                moment=moment,
                file_id=new_id(),
            )
            destination = resolve_storage_path(storage_key)
            await anyio.to_thread.run_sync(_write_bytes, destination, payload)

            try:
                document = Document(
                    file_name=_report_file_name(str(definition.key), moment),
                    storage_key=storage_key,
                    mime_type=XLSX_MIME_TYPE,
                    size_bytes=len(payload),
                    checksum_sha256=checksum,
                    category=DocumentCategory.REPORT,
                    entity_type=GENERATED_REPORT_ENTITY,
                    entity_id=snapshot.id,
                    uploaded_by_id=tenant.user_id,
                    is_public=False,
                    expires_at=default_expiry(DocumentCategory.REPORT, moment),
                )
                DocumentRepository(session, tenant).add(document)
                await session.flush()

                await reports_service.complete_snapshot(
                    session,
                    tenant,
                    snapshot_id=snapshot.id,
                    row_count=len(rows),
                    document_id=document.id,
                )

                async with transaction(session):
                    await notify(
                        session,
                        company_id=tenant.company_id,
                        user_ids=[tenant.user_id],
                        notification_type=NotificationType.REPORT_READY,
                        severity=NotificationSeverity.INFO,
                        title=f"{definition.title} is ready",
                        body=(
                            f"{len(rows)} rows covering {period_text}. "
                            "Open the job to download the spreadsheet."
                        ),
                        entity_type=GENERATED_REPORT_ENTITY,
                        entity_id=snapshot.id,
                        action_url=reports_service.job_status_url(snapshot.id),
                    )
            except Exception:
                # The row never committed, so nothing will ever reference these
                # bytes. Removing them here is the only cheap chance to do so.
                await anyio.to_thread.run_sync(_unlink, destination)
                raise

            document_id = str(document.id)
            row_count = len(rows)
            size_bytes = document.size_bytes

    except Exception as exc:
        # A separate session: the one the failure happened on cannot be used to
        # write anything, so the job would otherwise stay RUNNING for ever.
        async with session_scope() as session:
            await reports_service.fail_snapshot(
                session, tenant, snapshot_id=identifier, error=_failure_message(exc)
            )
        log.error("reports.generate_failed", snapshot_id=snapshot_id, error=str(exc), exc_info=exc)
        raise

    log.info(
        "reports.generated",
        snapshot_id=snapshot_id,
        document_id=document_id,
        rows=row_count,
        size_bytes=size_bytes,
    )
    return {
        "snapshot_id": snapshot_id,
        "status": "COMPLETED",
        "document_id": document_id,
        "row_count": row_count,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
    }


@async_task(name="reports.generate_purchase_order", queue="reports")
async def generate_purchase_order(
    purchase_order_id: str,
    snapshot_id: str,
    company_id: str,
    user_id: str,
    request_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Render one purchase order's PDF and file it in the document store.

    The job row (``snapshot_id``) is driven through the same lifecycle as every
    other background report: claimed on entry, completed with the document id
    once the row is written, and failed on a fresh session if anything raises -
    see :func:`generate_report` for why each of those matters. Without it the
    client polls a job that stays QUEUED for ever.

    Arguments are plain strings because Celery serialises task payloads as JSON;
    ``request_id`` is not used here but is read by the ``task_prerun`` signal so
    the API's correlation id follows the work into the worker's logs (section 55).
    """
    from src.modules.procurement.purchase_order_pdf_repository import purchase_order_pdf_data

    tenant = TenantContext(company_id=uuid.UUID(company_id), user_id=uuid.UUID(user_id))
    order_id = uuid.UUID(purchase_order_id)
    identifier = uuid.UUID(snapshot_id)
    moment = utcnow()

    try:
        async with session_scope() as session:
            try:
                snapshot = await reports_service.start_snapshot(session, tenant, identifier)
            except InvalidStateTransitionError:
                log.warning("reports.purchase_order_not_queued", snapshot_id=snapshot_id)
                return {
                    "snapshot_id": snapshot_id,
                    "status": "SKIPPED",
                    "reason": "the job is no longer queued",
                }

            data = await purchase_order_pdf_data(session, tenant, order_id)

            payload = await anyio.to_thread.run_sync(render_purchase_order_pdf, data)
            checksum = hashlib.sha256(payload).hexdigest()

            rule = KNOWN_TYPES[PDF_MIME_TYPE]
            storage_key = build_storage_key(
                category=DocumentCategory.REPORT,
                company_id=tenant.company_id,
                rule=rule,
                moment=moment,
                file_id=new_id(),
            )
            destination = resolve_storage_path(storage_key)
            await anyio.to_thread.run_sync(_write_bytes, destination, payload)

            try:
                document = Document(
                    file_name=f"purchase-order-{str(data.get('code', '')).replace('/', '-')}.pdf",
                    storage_key=storage_key,
                    mime_type=PDF_MIME_TYPE,
                    size_bytes=len(payload),
                    checksum_sha256=checksum,
                    category=DocumentCategory.REPORT,
                    entity_type=PURCHASE_ORDER_ENTITY,
                    entity_id=order_id,
                    uploaded_by_id=tenant.user_id,
                    is_public=False,
                    expires_at=None,
                )
                DocumentRepository(session, tenant).add(document)
                await session.flush()

                await record_audit(
                    session,
                    company_id=tenant.company_id,
                    action=AuditAction.DATA_EXPORT,
                    entity_type="purchase_order",
                    entity_id=order_id,
                    entity_code=str(data.get("code", "")),
                    summary=(
                        f"Generated the purchase order PDF for {data.get('code', '')} "
                        f"({len(payload)} bytes)"
                    ),
                    user_id=tenant.user_id,
                    new_values={
                        "document_id": document.id,
                        "file_name": document.file_name,
                        "size_bytes": document.size_bytes,
                        "checksum_sha256": checksum,
                        "line_count": len(data.get("lines", [])),
                    },
                )

                await reports_service.complete_snapshot(
                    session,
                    tenant,
                    snapshot_id=snapshot.id,
                    row_count=len(data.get("lines", [])),
                    document_id=document.id,
                )
            except Exception:
                # The row never committed, so nothing will ever reference these
                # bytes. Removing them here is the only cheap chance to do so.
                await anyio.to_thread.run_sync(_unlink, destination)
                raise

            document_id = str(document.id)
            size_bytes = document.size_bytes

    except Exception as exc:
        # A separate session: the one the failure happened on cannot be used to
        # write anything, so the job would otherwise stay RUNNING for ever.
        async with session_scope() as session:
            await reports_service.fail_snapshot(
                session, tenant, snapshot_id=identifier, error=_failure_message(exc)
            )
        log.error(
            "reports.purchase_order_failed", snapshot_id=snapshot_id, error=str(exc), exc_info=exc
        )
        raise

    log.info(
        "reports.purchase_order_generated",
        snapshot_id=snapshot_id,
        purchase_order_id=purchase_order_id,
        document_id=document_id,
        size_bytes=size_bytes,
    )
    return {
        "snapshot_id": snapshot_id,
        "status": "COMPLETED",
        "purchase_order_id": purchase_order_id,
        "purchase_order_code": str(data.get("code", "")),
        "document_id": document_id,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
        "line_count": len(data.get("lines", [])),
    }


__all__ = [
    "CARTONS_PER_PAGE",
    "GENERATED_REPORT_ENTITY",
    "MAX_VALUATION_EXPORT_ROWS",
    "PACKING_LIST_ENTITY",
    "PURCHASE_ORDER_ENTITY",
    "REPORT_COLUMN_WIDTH_LIMIT",
    "VALUATION_CHUNK_ROWS",
    "VALUATION_ENTITY",
    "generate_packing_list",
    "generate_purchase_order",
    "generate_report",
    "indian_money",
    "inventory_valuation",
    "plain_quantity",
    "render_inventory_valuation_xlsx",
    "render_packing_list_pdf",
    "render_purchase_order_pdf",
    "render_report_xlsx",
]
