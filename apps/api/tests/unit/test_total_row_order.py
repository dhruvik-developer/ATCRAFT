"""Orderings that OFFSET paging or a first-row pick depends on are total.

``core.pagination`` completes a single-entity list's ORDER BY with the primary
key (``test_core_pagination_order.py``). A report is a join or a grouped
projection, which it cannot complete, so each report orders itself - and an
order made only of readable keys two rows can share (a value, a name, a date)
returns tied rows in any order, not the same order twice: page 2 repeats a row
page 1 showed and drops another. A first-row pick such as ``DISTINCT ON`` has
the same defect in another shape: two runs pick different rows.

No database is touched here: each case compiles the statement and reads its
outermost ORDER BY. The behaviour on a real tie lives in
``tests/integration/test_reports_summary.py`` (the valuation, dead-stock and MRP
shortage pages) and ``tests/integration/test_sales_fulfilment.py`` (the latest
tracking event).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext
from src.modules.bom.repository import ItemLookupRepository
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import ReportFilters, ReportPeriod

pytestmark = pytest.mark.unit

TENANT = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
PERIOD = ReportPeriod(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)


def _order_by(stmt: Any) -> str:
    """The outermost ORDER BY of a statement, as SQL on one line."""
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ORDER BY" in sql, sql
    return " ".join(sql.rsplit("ORDER BY", 1)[1].split())


#: (report, statement builder, the trailing terms that make its order total).
REPORT_ORDERS: list[tuple[str, Callable[[ReportQueryRepository], Any], str]] = [
    (
        # One row per run and item across every run in the period.
        "mrp shortage",
        lambda repo: repo.mrp_shortage(PERIOD, ReportFilters()),
        "mrp_requirements.item_code ASC, mrp_requirements.id ASC",
    ),
    (
        # Grouped by (item, vendor); vendor names are not unique.
        "rework rate",
        lambda repo: repo.rework_rate(PERIOD, ReportFilters()),
        "vendors.name ASC, qc_inspections.vendor_id ASC NULLS LAST",
    ),
    (
        # Grouped by (month, transporter, courier); transporter names are not unique.
        "inbound freight",
        lambda repo: repo.inbound_freight(PERIOD, ReportFilters()),
        "purchase_order_shipments.courier_name ASC NULLS LAST, transporters.id ASC NULLS LAST",
    ),
    (
        # (item, warehouse) grain; both codes are unique per company.
        "dead stock",
        lambda repo: repo.dead_stock(ReportFilters(), cutoff=CUTOFF),
        "items.code ASC, warehouses.code ASC",
    ),
    (
        "inventory valuation",
        lambda repo: repo.inventory_valuation(ReportFilters()),
        "items.code ASC, warehouses.code ASC",
    ),
]


@pytest.mark.parametrize(
    ("build", "tail"),
    [pytest.param(build, tail, id=name) for name, build, tail in REPORT_ORDERS],
)
def test_a_report_page_order_ends_on_a_unique_key(
    build: Callable[[ReportQueryRepository], Any], tail: str
) -> None:
    order = _order_by(build(ReportQueryRepository(None, TENANT)))

    assert order.endswith(tail), order


class _CapturingSession:
    """Records each statement it is asked to run and answers with no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> list[Any]:
        self.statements.append(stmt)
        return []


async def test_the_fallback_purchase_price_is_picked_the_same_way_every_run() -> None:
    """Two non-preferred links bought the same day tie; the link id decides.

    BOM approval persists the material cost rolled up from this price, so an
    arbitrary pick would store a different cost for the same structure.
    """
    session = _CapturingSession()

    await ItemLookupRepository(session, TENANT).last_purchase_prices([uuid.uuid4()])

    (stmt,) = session.statements
    assert _order_by(stmt).endswith(
        "item_vendor_links.last_purchase_date DESC NULLS LAST, item_vendor_links.id DESC"
    )
