"""The shift master and the roster against real PostgreSQL.

What is proved here is what a unit test cannot see: that every reference is read
in the tenant, that every clash in a request is named before anything is
written, that the EXCLUDE constraints really refuse what the service would have,
and that a shift, a machine or a work centre still on a future plan cannot be
re-timed, retired or moved out from under it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.messages import SHIFT_ASSIGNMENT_OVERLAP
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.routing import service as routing_service
from src.modules.routing.models import MachineStatus, ShiftAssignment, ShiftAssignmentStatus
from src.modules.routing.schemas import (
    MachineStatusChange,
    MachineUpdate,
    ShiftAssignmentCreate,
    ShiftAssignmentUpdate,
    ShiftCreate,
    ShiftFilters,
    ShiftRosterCopyRequest,
    ShiftRosterFilters,
    ShiftUpdate,
)
from src.modules.routing.service import (
    MachineService,
    ShiftRosterService,
    ShiftService,
    WorkCenterService,
    shift_window,
)
from tests.seed_support import (
    seed_company,
    seed_machine,
    seed_shift,
    seed_user,
    seed_vendor,
    seed_work_center,
)

pytestmark = pytest.mark.integration

#: Far enough ahead that it is "from today onwards" in any timezone.
DAY = today() + timedelta(days=5)


@dataclass
class Scene:
    session: AsyncSession
    tenant: TenantContext
    centre: uuid.UUID
    centre_code: str
    machine: uuid.UUID
    machine_code: str
    shift_a: uuid.UUID
    shift_b: uuid.UUID
    general: uuid.UUID
    amit: uuid.UUID
    neha: uuid.UUID

    @property
    def roster(self) -> ShiftRosterService:
        return ShiftRosterService(self.session, self.tenant)

    @property
    def shifts(self) -> ShiftService:
        return ShiftService(self.session, self.tenant)

    def plan(
        self,
        operator: uuid.UUID,
        shift: uuid.UUID,
        *,
        day: date = DAY,
        centre: uuid.UUID | None = None,
        machine: uuid.UUID | None = None,
        notes: str | None = None,
    ) -> ShiftAssignmentCreate:
        return ShiftAssignmentCreate(
            roster_date=day,
            shift_id=shift,
            work_center_id=centre or self.centre,
            machine_id=machine,
            operator_user_id=operator,
            notes=notes,
        )

    async def listed(self, day: date = DAY, **filters: object) -> list[uuid.UUID]:
        rows, _ = await self.roster.list_assignments(
            ShiftRosterFilters.model_validate({"date_from": day, "date_to": day, **filters}),
            PageParams(),
        )
        return [row.id for row in rows]


@pytest.fixture
async def scene(session: AsyncSession) -> Scene:
    company = await seed_company(session)
    actor = await seed_user(session, company_id=company.id)
    centre = await seed_work_center(session, company_id=company.id)
    machine = await seed_machine(session, company_id=company.id, work_center_id=centre.id)
    shift_a = await seed_shift(session, company_id=company.id, code="A")
    shift_b = await seed_shift(
        session, company_id=company.id, code="B", start_time=time(14), end_time=time(22)
    )
    general = await seed_shift(
        session,
        company_id=company.id,
        code="GEN",
        name="General",
        start_time=time(9),
        end_time=time(17, 30),
        break_minutes=30,
    )
    amit = await seed_user(
        session, company_id=company.id, full_name="Amit Shah", employee_code="EMP-OP-001"
    )
    neha = await seed_user(
        session, company_id=company.id, full_name="Neha Patel", employee_code="EMP-OP-002"
    )
    return Scene(
        session=session,
        tenant=TenantContext(company_id=company.id, user_id=actor.id),
        centre=centre.id,
        centre_code=centre.code,
        machine=machine.id,
        machine_code=machine.code,
        shift_a=shift_a.id,
        shift_b=shift_b.id,
        general=general.id,
        amit=amit.id,
        neha=neha.id,
    )


async def audit_rows(session: AsyncSession, entity_id: uuid.UUID) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).scalars().all())


# --- the shift master --------------------------------------------------------


async def test_a_night_shift_is_created_with_its_midnight_flag_derived(scene: Scene) -> None:
    created = await scene.shifts.create_shift(
        ShiftCreate(code=" c ", name="Night", start_time=time(22), end_time=time(6))
    )
    assert (created.code, created.crosses_midnight) == ("C", True)
    audits = await audit_rows(scene.session, created.id)
    assert [audit.action for audit in audits] == [AuditAction.CREATE]


async def test_listing_shifts_lists_what_exists_in_board_order(scene: Scene) -> None:
    rows, page = await scene.shifts.list_shifts(ShiftFilters(), PageParams(), SortParams())
    assert [row.code for row in rows] == ["A", "B", "GEN"]
    assert page.total == 3
    found, _ = await scene.shifts.list_shifts(
        ShiftFilters(search="gen", is_active=True), PageParams(), SortParams()
    )
    assert [row.code for row in found] == ["GEN"]


async def test_a_shift_code_is_never_reused_even_after_retirement(scene: Scene) -> None:
    await scene.shifts.retire_shift(scene.general, reason="Replaced by a shorter day")
    with pytest.raises(DuplicateError):
        await scene.shifts.create_shift(
            ShiftCreate(code="GEN", name="General", start_time=time(9), end_time=time(17))
        )


async def test_a_new_break_is_checked_against_the_merged_times(scene: Scene) -> None:
    # GEN is 09:00-17:30 with a 30-minute break; moving only the end to 09:20
    # would leave a break longer than the shift.
    with pytest.raises(ValidationError) as caught:
        await scene.shifts.update_shift(scene.general, ShiftUpdate(end_time=time(9, 20)))
    assert set(caught.value.details["fields"]) == {"break_minutes"}


# --- planning ----------------------------------------------------------------


async def test_a_plan_is_written_audited_and_listed_with_its_labels(scene: Scene) -> None:
    rows, message = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=scene.machine, notes="Lathe")]
    )
    row = rows[0]

    assert message == "1 assignment planned."
    assert (row.shift_code, row.machine_code, row.operator_name, row.employee_code) == (
        "A",
        scene.machine_code,
        "Amit Shah",
        "EMP-OP-001",
    )
    assert row.status is ShiftAssignmentStatus.PLANNED
    # The stored window is the company-timezone window of the local start date.
    shift = await scene.shifts.repo.get_or_404(scene.shift_a)
    assert (row.starts_at, row.ends_at) == shift_window(DAY, shift, "Asia/Kolkata")

    assert await scene.listed() == [row.id]
    audits = await audit_rows(scene.session, row.id)
    assert [audit.action for audit in audits] == [AuditAction.CREATE]
    assert audits[0].entity_code == f"{DAY.isoformat()} A"


async def test_another_tenants_references_are_not_found(scene: Scene) -> None:
    other = await seed_company(scene.session)
    foreign_shift = await seed_shift(scene.session, company_id=other.id, code="A")
    foreign_centre = await seed_work_center(scene.session, company_id=other.id)
    foreign_machine = await seed_machine(
        scene.session, company_id=other.id, work_center_id=foreign_centre.id
    )
    foreign_user = await seed_user(scene.session, company_id=other.id)

    for payload in (
        scene.plan(scene.amit, foreign_shift.id),
        scene.plan(scene.amit, scene.shift_a, centre=foreign_centre.id),
        scene.plan(scene.amit, scene.shift_a, machine=foreign_machine.id),
        scene.plan(foreign_user.id, scene.shift_a),
    ):
        with pytest.raises(NotFoundError):
            await scene.roster.plan([payload])
    assert await scene.listed() == []


async def test_an_inactive_or_vendor_portal_operator_is_refused(scene: Scene) -> None:
    gone = await seed_user(
        scene.session, company_id=scene.tenant.company_id, full_name="Left", is_active=False
    )
    vendor = await seed_vendor(scene.session, company_id=scene.tenant.company_id)
    portal = await seed_user(
        scene.session,
        company_id=scene.tenant.company_id,
        full_name="Portal User",
        vendor_id=vendor.id,
    )
    with pytest.raises(BusinessRuleError, match="inactive"):
        await scene.roster.plan([scene.plan(gone.id, scene.shift_a)])
    with pytest.raises(BusinessRuleError, match="vendor-portal"):
        await scene.roster.plan([scene.plan(portal.id, scene.shift_a)])


async def test_a_machine_must_be_in_service_and_in_the_stated_cell(scene: Scene) -> None:
    company = scene.tenant.company_id
    elsewhere = await seed_work_center(scene.session, company_id=company)
    stray = await seed_machine(scene.session, company_id=company, work_center_id=elsewhere.id)
    retired = await seed_machine(
        scene.session,
        company_id=company,
        work_center_id=scene.centre,
        status=MachineStatus.RETIRED,
        is_active=False,
    )
    with pytest.raises(BusinessRuleError, match="does not belong to work centre"):
        await scene.roster.plan([scene.plan(scene.amit, scene.shift_a, machine=stray.id)])
    with pytest.raises(BusinessRuleError, match="retired or inactive"):
        await scene.roster.plan([scene.plan(scene.amit, scene.shift_a, machine=retired.id)])


async def test_a_machine_under_maintenance_is_planned_with_a_warning(scene: Scene) -> None:
    serviced = await seed_machine(
        scene.session,
        company_id=scene.tenant.company_id,
        work_center_id=scene.centre,
        status=MachineStatus.MAINTENANCE,
    )
    rows, message = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=serviced.id)]
    )
    assert len(rows) == 1
    assert "Warning:" in message and serviced.code in message and "maintenance" in message


async def test_an_inactive_shift_or_cell_cannot_be_planned(scene: Scene) -> None:
    await scene.shifts.set_active(scene.general, active=False, reason="Seasonal")
    with pytest.raises(BusinessRuleError, match="inactive"):
        await scene.roster.plan([scene.plan(scene.amit, scene.general)])

    closed = await seed_work_center(
        scene.session, company_id=scene.tenant.company_id, is_active=False
    )
    with pytest.raises(BusinessRuleError, match="inactive"):
        await scene.roster.plan([scene.plan(scene.amit, scene.shift_a, centre=closed.id)])


async def test_back_to_back_shifts_may_share_one_machine(scene: Scene) -> None:
    rows, message = await scene.roster.plan(
        [
            scene.plan(scene.amit, scene.shift_a, machine=scene.machine),
            scene.plan(scene.neha, scene.shift_b, machine=scene.machine),
        ]
    )
    assert message == "2 assignments planned."
    assert [row.shift_code for row in rows] == ["A", "B"]


async def test_every_clash_is_named_and_nothing_is_written(scene: Scene) -> None:
    planned, _ = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=scene.machine)]
    )
    with pytest.raises(BusinessRuleError) as caught:
        await scene.roster.plan(
            [
                # Amit is already on A; the general shift overlaps it.
                scene.plan(scene.amit, scene.general),
                # The lathe is already staffed on A.
                scene.plan(scene.neha, scene.shift_a, machine=scene.machine),
                # Fine on its own: Neha on B, no machine.
                scene.plan(scene.neha, scene.shift_b),
            ]
        )
    error = caught.value
    assert error.message == SHIFT_ASSIGNMENT_OVERLAP
    conflicts = error.details["conflicts"]
    assert {(item["index"], item["resource"]) for item in conflicts} == {
        (0, "operator"),
        (1, "machine"),
    }
    assert {item["conflicting_assignment_id"] for item in conflicts} == {str(planned[0].id)}
    assert all(item["message"] for item in conflicts)
    # All or nothing: the valid third row was not written either.
    assert await scene.listed() == [planned[0].id]


async def test_a_clash_inside_one_request_is_named_too(scene: Scene) -> None:
    with pytest.raises(BusinessRuleError) as caught:
        await scene.roster.plan(
            [scene.plan(scene.neha, scene.shift_a), scene.plan(scene.neha, scene.general)]
        )
    (clash,) = caught.value.details["conflicts"]
    assert (clash["index"], clash["resource"], clash["conflicting_assignment_id"]) == (
        1,
        "operator",
        None,
    )
    assert "this same request" in clash["message"]


# --- re-staffing and cancelling ------------------------------------------------


async def test_re_staffing_never_clashes_with_itself_and_is_revalidated(scene: Scene) -> None:
    rows, _ = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=scene.machine)]
    )
    row_id = rows[0].id

    noted, message = await scene.roster.restaff(row_id, ShiftAssignmentUpdate(notes="Covering"))
    assert (noted.notes, message) == ("Covering", "Assignment updated.")

    swapped, _ = await scene.roster.restaff(
        row_id, ShiftAssignmentUpdate(operator_user_id=scene.neha)
    )
    assert swapped.operator_name == "Neha Patel"
    assert (swapped.starts_at, swapped.ends_at) == (rows[0].starts_at, rows[0].ends_at)

    # Amit is free now; plan him on the general shift, then try to move the row
    # back onto him - he is busy, and the change is refused.
    await scene.roster.plan([scene.plan(scene.amit, scene.general)])
    with pytest.raises(BusinessRuleError) as caught:
        await scene.roster.restaff(row_id, ShiftAssignmentUpdate(operator_user_id=scene.amit))
    assert {item["resource"] for item in caught.value.details["conflicts"]} == {"operator"}

    cell_level, _ = await scene.roster.restaff(row_id, ShiftAssignmentUpdate(machine_id=None))
    assert cell_level.machine_id is None

    audits = await audit_rows(scene.session, row_id)
    assert [audit.action for audit in audits].count(AuditAction.UPDATE) == 3


async def test_cancel_is_audited_with_its_reason_and_frees_the_window(scene: Scene) -> None:
    rows, _ = await scene.roster.plan([scene.plan(scene.amit, scene.shift_a)])
    row_id = rows[0].id

    cancelled = await scene.roster.cancel(row_id, reason="Called in sick")
    assert cancelled.status is ShiftAssignmentStatus.CANCELLED
    audit = (await audit_rows(scene.session, row_id))[-1]
    assert audit.action == AuditAction.STATUS_CHANGE
    assert audit.context == {"reason": "Called in sick"}
    assert audit.new_values == {"status": "CANCELLED"}

    # The window is free again, and the cancelled row stays readable.
    again, _ = await scene.roster.plan([scene.plan(scene.amit, scene.shift_a)])
    assert set(await scene.listed()) == {row_id, again[0].id}
    assert await scene.listed(status=ShiftAssignmentStatus.PLANNED) == [again[0].id]

    with pytest.raises(InvalidStateTransitionError):
        await scene.roster.cancel(row_id, reason="Twice")
    with pytest.raises(BusinessRuleError, match="cancelled"):
        await scene.roster.restaff(row_id, ShiftAssignmentUpdate(notes="Too late"))


# --- copying a day -------------------------------------------------------------


async def test_copy_day_plans_the_range_and_refuses_as_a_whole(scene: Scene) -> None:
    await scene.roster.plan(
        [
            scene.plan(scene.amit, scene.shift_a, machine=scene.machine),
            scene.plan(scene.neha, scene.shift_b, machine=scene.machine),
        ]
    )
    copied, message = await scene.roster.copy_day(
        ShiftRosterCopyRequest(
            source_date=DAY,
            target_from=DAY + timedelta(days=1),
            target_to=DAY + timedelta(days=3),
        )
    )
    assert (len(copied), message) == (6, "6 assignments planned.")
    assert {row.roster_date for row in copied} == {DAY + timedelta(days=n) for n in (1, 2, 3)}

    # Copying onto days that are already planned clashes everywhere at once.
    with pytest.raises(BusinessRuleError) as caught:
        await scene.roster.copy_day(
            ShiftRosterCopyRequest(
                source_date=DAY,
                target_from=DAY + timedelta(days=2),
                target_to=DAY + timedelta(days=4),
            )
        )
    assert len(caught.value.details["conflicts"]) >= 4
    assert await scene.listed(day=DAY + timedelta(days=4)) == []


async def test_copying_an_empty_day_or_too_much_is_refused(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    tomorrow = DAY + timedelta(days=1)
    with pytest.raises(BusinessRuleError, match="Nothing is planned"):
        await scene.roster.copy_day(
            ShiftRosterCopyRequest(source_date=DAY, target_from=tomorrow, target_to=tomorrow)
        )
    await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a), scene.plan(scene.neha, scene.shift_b)]
    )
    monkeypatch.setattr(routing_service, "MAX_ROSTER_BULK", 3)
    with pytest.raises(BusinessRuleError, match="Narrow the work centre or the dates"):
        await scene.roster.copy_day(
            ShiftRosterCopyRequest(
                source_date=DAY, target_from=tomorrow, target_to=tomorrow + timedelta(days=1)
            )
        )


# --- what a future plan protects --------------------------------------------


async def test_a_shift_on_a_future_plan_cannot_be_retimed_deactivated_or_retired(
    scene: Scene,
) -> None:
    rows, _ = await scene.roster.plan([scene.plan(scene.amit, scene.shift_a)])

    for attempt in (
        scene.shifts.update_shift(scene.shift_a, ShiftUpdate(start_time=time(7))),
        scene.shifts.update_shift(scene.shift_a, ShiftUpdate(break_minutes=15)),
        scene.shifts.set_active(scene.shift_a, active=False, reason="Changing the day"),
        scene.shifts.retire_shift(scene.shift_a, reason="Changing the day"),
    ):
        with pytest.raises(BusinessRuleError) as caught:
            await attempt
        assert caught.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    renamed = await scene.shifts.update_shift(scene.shift_a, ShiftUpdate(name="Early"))
    assert (renamed.code, renamed.name) == ("A", "Early")

    await scene.roster.cancel(rows[0].id, reason="Plan moved")
    retimed = await scene.shifts.update_shift(scene.shift_a, ShiftUpdate(start_time=time(7)))
    assert retimed.start_time == time(7)
    await scene.shifts.retire_shift(scene.shift_a, reason="Replaced")


async def test_a_plan_in_the_past_does_not_hold_the_shift(scene: Scene) -> None:
    await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_b, day=today() - timedelta(days=10))]
    )
    retimed = await scene.shifts.update_shift(scene.shift_b, ShiftUpdate(end_time=time(23)))
    assert retimed.end_time == time(23)


async def test_a_machine_on_a_future_plan_is_not_retired_or_moved(scene: Scene) -> None:
    rows, _ = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=scene.machine)]
    )
    machines = MachineService(scene.session, scene.tenant)
    elsewhere = await seed_work_center(scene.session, company_id=scene.tenant.company_id)

    for attempt in (
        machines.retire_machine(scene.machine, reason="Scrapped"),
        machines.change_status(
            scene.machine, MachineStatusChange(status=MachineStatus.RETIRED, reason="Scrapped")
        ),
        machines.update_machine(scene.machine, MachineUpdate(work_center_id=elsewhere.id)),
    ):
        with pytest.raises(BusinessRuleError) as caught:
            await attempt
        assert caught.value.code == ErrorCode.REFERENCED_RECORD_IN_USE
        assert scene.machine_code in caught.value.message

    # Once the plan is cancelled the machine may be retired - but never moved:
    # the cancelled row still records it at this centre, which is what the
    # composite foreign key protects.
    await scene.roster.cancel(rows[0].id, reason="Machine going")
    with pytest.raises(BusinessRuleError, match="cannot be moved") as refused:
        await machines.update_machine(scene.machine, MachineUpdate(work_center_id=elsewhere.id))
    assert refused.value.code == ErrorCode.REFERENCED_RECORD_IN_USE
    await machines.retire_machine(scene.machine, reason="Scrapped")


async def test_a_machine_with_no_roster_history_moves_freely(scene: Scene) -> None:
    machines = MachineService(scene.session, scene.tenant)
    spare = await seed_machine(
        scene.session, company_id=scene.tenant.company_id, work_center_id=scene.centre
    )
    elsewhere = await seed_work_center(scene.session, company_id=scene.tenant.company_id)
    moved = await machines.update_machine(spare.id, MachineUpdate(work_center_id=elsewhere.id))
    assert moved.work_center_id == elsewhere.id


async def test_a_work_centre_on_a_future_plan_is_not_retired(scene: Scene) -> None:
    bench = await seed_work_center(scene.session, company_id=scene.tenant.company_id)
    rows, _ = await scene.roster.plan([scene.plan(scene.amit, scene.general, centre=bench.id)])
    centres = WorkCenterService(scene.session, scene.tenant)

    with pytest.raises(BusinessRuleError) as caught:
        await centres.retire_work_center(bench.id, reason="Closing the bench")
    assert caught.value.code == ErrorCode.REFERENCED_RECORD_IN_USE

    await scene.roster.cancel(rows[0].id, reason="Bench closing")
    await centres.retire_work_center(bench.id, reason="Closing the bench")


# --- reading -------------------------------------------------------------------


async def test_the_roster_list_filters_and_orders_by_date_then_start(scene: Scene) -> None:
    rows, _ = await scene.roster.plan(
        [
            scene.plan(scene.neha, scene.shift_b, day=DAY + timedelta(days=1)),
            scene.plan(scene.amit, scene.shift_b, machine=scene.machine),
            scene.plan(scene.neha, scene.shift_a),
        ]
    )
    # Written rows come back in board order, whatever order they were sent in.
    assert [(row.roster_date, row.shift_code) for row in rows] == [
        (DAY, "A"),
        (DAY, "B"),
        (DAY + timedelta(days=1), "B"),
    ]
    neha_on_a, amit_on_b, _ = rows

    window = ShiftRosterFilters(date_from=DAY, date_to=DAY + timedelta(days=1))
    listed, page = await scene.roster.list_assignments(window, PageParams())
    assert [row.id for row in listed] == [row.id for row in rows]
    assert page.total == 3 and not page.has_next
    assert await scene.listed(operator_user_id=scene.amit) == [amit_on_b.id]
    assert await scene.listed(machine_id=scene.machine) == [amit_on_b.id]
    assert await scene.listed(shift_id=scene.shift_a) == [neha_on_a.id]
    assert await scene.listed(work_center_id=uuid.uuid4()) == []

    first, paged = await scene.roster.list_assignments(window, PageParams(limit=2))
    assert len(first) == 2 and paged.has_next


async def test_the_operator_directory_offers_active_internal_people_only(scene: Scene) -> None:
    company = scene.tenant.company_id
    await seed_user(scene.session, company_id=company, full_name="Gone Away", is_active=False)
    vendor = await seed_vendor(scene.session, company_id=company)
    await seed_user(
        scene.session, company_id=company, full_name="Vendor Login", vendor_id=vendor.id
    )
    other = await seed_company(scene.session)
    await seed_user(scene.session, company_id=other.id, full_name="Amit Elsewhere")

    people, _ = await scene.roster.list_operators(None, PageParams())
    names = [person.full_name for person in people]
    assert "Amit Shah" in names and "Neha Patel" in names
    assert not {"Gone Away", "Vendor Login", "Amit Elsewhere"} & set(names)

    found, _ = await scene.roster.list_operators("emp-op-002", PageParams())
    assert [person.full_name for person in found] == ["Neha Patel"]


# --- the database backstop ---------------------------------------------------


@pytest.mark.parametrize("resource", ["operator", "machine"])
async def test_the_exclusion_constraints_refuse_what_the_service_would(
    scene: Scene, resource: str
) -> None:
    """Written past the service, an overlapping PLANNED window still cannot land."""
    rows, _ = await scene.roster.plan(
        [scene.plan(scene.amit, scene.shift_a, machine=scene.machine)]
    )
    stored = rows[0]
    with pytest.raises(IntegrityError) as caught:
        async with transaction(scene.session):
            scene.session.add(
                ShiftAssignment(
                    company_id=scene.tenant.company_id,
                    roster_date=stored.roster_date,
                    shift_id=scene.general,
                    work_center_id=scene.centre,
                    machine_id=scene.machine if resource == "machine" else None,
                    operator_user_id=scene.amit if resource == "operator" else scene.neha,
                    starts_at=stored.starts_at + timedelta(hours=1),
                    ends_at=stored.ends_at + timedelta(hours=1),
                )
            )
            await scene.session.flush()
    original = caught.value.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    assert (sqlstate or getattr(original.__cause__, "sqlstate", None)) == "23P01"
    assert f"ex_shift_assignments_{resource}_window" in str(original)
