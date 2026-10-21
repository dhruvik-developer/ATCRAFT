"""Receipt-side guards that need no database.

Three rules, each reachable through the API in the wrong hands:

* Loss beyond the agreed allowance is a vendor debit, so the flag that accepts
  it is honoured only from a ``job_work.close`` holder. The receive route is
  gated on ``job_work.receive`` alone, and before this guard the storekeeper
  keying the lot could wave the excess through and be recorded as its approver.
* A verdict that leaves part of a hold undecided is refused with the item's
  code in the sentence, not its uuid - the frontend shows messages verbatim.
* The charges feed pages instead of silently cutting a financial extract at
  the vendor cap, and an empty vendor restriction matches nobody.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import Principal, TenantContext
from src.core.errors import (
    ApprovalRequiredError,
    BusinessRuleError,
    ErrorCode,
    PermissionDeniedError,
    ValidationError,
)
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.modules.job_work import service
from src.modules.job_work.repository import JobWorkReceiptRepository
from src.modules.job_work.service import (
    LineReceiptPlan,
    PlannedLine,
    assert_excess_loss_authorised,
    assert_verdict_covers_hold,
)

pytestmark = pytest.mark.unit

ORDER_CODE = "JW/24/0007"


def _principal(*permissions: Permission) -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="stores@test.local",
        full_name="Stores",
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
    )


def _planned(line_number: int, excess: str) -> PlannedLine:
    """One line of a lot: 100 sent, 90 back, 10 scrapped, ``excess`` of it unagreed."""
    plan = LineReceiptPlan(
        line_number=line_number,
        output_quantity=Decimal("90"),
        rejected_quantity=Decimal("0"),
        rework_quantity=Decimal("0"),
        returned_unprocessed_quantity=Decimal("0"),
        input_for_output=Decimal("90"),
        scrap_quantity=Decimal("10"),
        excess_loss_quantity=Decimal(excess),
        input_consumed_quantity=Decimal("100"),
    )
    return PlannedLine(
        order_line=SimpleNamespace(line_number=line_number),  # type: ignore[arg-type]
        entry=SimpleNamespace(),  # type: ignore[arg-type]
        plan=plan,
        batch_id=None,
    )


# -----------------------------------------------------------------------------
# Excess loss: the flag is not the approval, the right is
# -----------------------------------------------------------------------------


class TestExcessLossAuthority:
    def test_loss_inside_the_allowance_needs_nobody(self) -> None:
        excess = assert_excess_loss_authorised(
            _principal(Permission.JOB_WORK_RECEIVE),
            order_code=ORDER_CODE,
            planned=[_planned(1, "0"), _planned(2, "0")],
            approve_excess_loss=False,
        )
        assert excess == Decimal("0")

    def test_excess_without_the_flag_needs_approval_whoever_is_keying(self) -> None:
        # Even the person who could approve it has to say so explicitly.
        with pytest.raises(ApprovalRequiredError) as exc:
            assert_excess_loss_authorised(
                _principal(Permission.JOB_WORK_RECEIVE, Permission.JOB_WORK_CLOSE),
                order_code=ORDER_CODE,
                planned=[_planned(1, "8"), _planned(2, "0")],
                approve_excess_loss=False,
            )

        assert exc.value.code is ErrorCode.APPROVAL_REQUIRED
        assert ORDER_CODE in exc.value.message
        assert exc.value.details["order_code"] == ORDER_CODE
        assert exc.value.details["excess_loss_quantity"] == "8.0000"
        # Only the lines that carry excess are named.
        assert exc.value.details["lines"] == {"1": "8"}

    def test_the_flag_from_a_storekeeper_is_not_an_approval(self) -> None:
        """``job_work.receive`` is the route's only gate; it must not be enough."""
        with pytest.raises(PermissionDeniedError) as exc:
            assert_excess_loss_authorised(
                _principal(Permission.JOB_WORK_RECEIVE),
                order_code=ORDER_CODE,
                planned=[_planned(1, "8")],
                approve_excess_loss=True,
            )

        assert exc.value.code is ErrorCode.PERMISSION_DENIED
        assert exc.value.details["required_permission"] == str(Permission.JOB_WORK_CLOSE)

    def test_the_flag_from_a_close_holder_accepts_the_excess(self) -> None:
        excess = assert_excess_loss_authorised(
            _principal(Permission.JOB_WORK_RECEIVE, Permission.JOB_WORK_CLOSE),
            order_code=ORDER_CODE,
            planned=[_planned(1, "5"), _planned(2, "3")],
            approve_excess_loss=True,
        )
        # Summed across the lot and quantised, so the debit prices the whole excess.
        assert excess == Decimal("8.0000")

    def test_the_receive_path_takes_the_caller_not_the_flag_on_trust(self) -> None:
        """The service signature carries the principal - the route cannot forget it."""
        import inspect

        parameters = list(inspect.signature(service.receive).parameters)
        assert parameters[:5] == ["session", "tenant", "principal", "order_id", "payload"]


# -----------------------------------------------------------------------------
# Verdict coverage: name the part, not the uuid
# -----------------------------------------------------------------------------


class TestVerdictCoversHold:
    def test_a_covering_verdict_passes(self) -> None:
        assert_verdict_covers_hold(
            item_id=uuid.uuid4(),
            item_code="HNDL-PVD-01",
            held_quantity=Decimal("12.0000"),
            accepted=Decimal("10.0000"),
            rejected=Decimal("2.0000"),
        )

    def test_a_short_verdict_names_the_item_by_code(self) -> None:
        item_id = uuid.uuid4()
        with pytest.raises(BusinessRuleError) as exc:
            assert_verdict_covers_hold(
                item_id=item_id,
                item_code="HNDL-PVD-01",
                held_quantity=Decimal("12.0000"),
                accepted=Decimal("10.0000"),
                rejected=Decimal("0.0000"),
            )

        assert exc.value.message == (
            "12.0000 unit(s) of HNDL-PVD-01 are on hold; the verdict accounts for 10.0000."
        )
        assert str(item_id) not in exc.value.message
        assert exc.value.details["item_code"] == "HNDL-PVD-01"
        assert exc.value.details["item_id"] == str(item_id)
        assert exc.value.details["held_quantity"] == "12.0000"

    def test_an_item_retired_since_the_receipt_falls_back_to_its_id(self) -> None:
        item_id = uuid.uuid4()
        with pytest.raises(BusinessRuleError) as exc:
            assert_verdict_covers_hold(
                item_id=item_id,
                item_code=None,
                held_quantity=Decimal("12.0000"),
                accepted=Decimal("12.0000"),
                rejected=Decimal("1.0000"),
            )

        assert str(item_id) in exc.value.message
        assert exc.value.details["item_code"] == str(item_id)


# -----------------------------------------------------------------------------
# Charges: paged, never silently cut
# -----------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[Any] | None = None, scalar: int | None = None) -> None:
        self._rows = rows if rows is not None else []
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> int:
        assert self._scalar is not None
        return self._scalar


class _Session:
    """Answers the page statement first and the count statement second."""

    def __init__(self, rows: list[Any], total: int = 0) -> None:
        self._answers = [_Result(rows=rows), _Result(scalar=total)]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self._answers.pop(0)


class _RefusingSession:
    async def execute(self, statement: Any) -> _Result:
        raise AssertionError("no statement may run before the period is validated")


def _vendor_row(charges: str) -> SimpleNamespace:
    return SimpleNamespace(
        vendor_id=uuid.uuid4(),
        gross_charges=Decimal(charges),
        vendor_debits=Decimal("0"),
        receipt_count=1,
        order_count=1,
        output_quantity=Decimal("100"),
    )


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.fixture
def tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


class TestChargesPage:
    async def test_one_row_past_the_page_says_there_is_more(self, tenant: TenantContext) -> None:
        session = _Session([_vendor_row("300"), _vendor_row("200"), _vendor_row("100")])
        repo = JobWorkReceiptRepository(session, tenant)  # type: ignore[arg-type]

        rows, pagination = await repo.charges_page(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 31),
            vendor_ids=None,
            params=PageParams(page=1, limit=2, with_total=False),
        )

        # The extra row decided has_next and was not handed back.
        assert [row.gross_charges for row in rows] == [Decimal("300"), Decimal("200")]
        assert pagination.has_next is True
        assert pagination.has_previous is False
        assert pagination.total is None
        # Exactly one statement ran: the fence-post row is fetched with the page,
        # not by a second query, and no count runs unless asked for.
        assert len(session.statements) == 1
        compiled = session.statements[0].compile(dialect=postgresql.dialect())
        assert 3 in compiled.params.values()  # limit + 1

    async def test_the_last_page_carries_a_total_when_asked(self, tenant: TenantContext) -> None:
        session = _Session([_vendor_row("300"), _vendor_row("200")], total=4)
        repo = JobWorkReceiptRepository(session, tenant)  # type: ignore[arg-type]

        rows, pagination = await repo.charges_page(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 31),
            vendor_ids=None,
            params=PageParams(page=2, limit=2, with_total=True),
        )

        assert len(rows) == 2
        assert pagination.has_next is False
        assert pagination.has_previous is True
        assert pagination.total == 4
        assert pagination.total_pages == 2
        assert len(session.statements) == 2
        assert "count(" in _sql(session.statements[1])

    def test_no_vendor_restriction_means_every_vendor(self, tenant: TenantContext) -> None:
        repo = JobWorkReceiptRepository(_RefusingSession(), tenant)  # type: ignore[arg-type]
        sql = _sql(
            repo.charges_stmt(
                date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), vendor_ids=None
            )
        )
        assert "vendor_id IN" not in sql
        assert "job_work_receipts.company_id = " in sql

    def test_an_empty_vendor_restriction_matches_nobody(self, tenant: TenantContext) -> None:
        """``if xs: where(in_(xs))`` would drop the restriction and return everyone."""
        repo = JobWorkReceiptRepository(_RefusingSession(), tenant)  # type: ignore[arg-type]
        sql = _sql(
            repo.charges_stmt(date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), vendor_ids=[])
        )
        assert "vendor_id IN" in sql

    async def test_an_inverted_period_is_refused_before_any_query(
        self, tenant: TenantContext
    ) -> None:
        with pytest.raises(ValidationError) as exc:
            await service.list_charges_page(
                _RefusingSession(),  # type: ignore[arg-type]
                tenant,
                date_from=date(2026, 8, 31),
                date_to=date(2026, 8, 1),
                vendor_ids=None,
                params=PageParams(),
            )
        assert exc.value.message == "The period end cannot be before the period start."
        assert exc.value.details == {"date_from": "2026-08-31", "date_to": "2026-08-01"}
