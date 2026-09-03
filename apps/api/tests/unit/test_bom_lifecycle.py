"""BOM lifecycle state machine and the approval controls around it.

The rules under test are the ones that keep an approved structure trustworthy:
you cannot skip review, you cannot edit what production is already building
against, and you cannot approve your own work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

import pytest

from src.core.deps import Principal
from src.core.errors import InvalidStateTransitionError
from src.core.permissions import Permission
from src.modules.bom.models import BomStatus
from src.modules.bom.service import (
    BOM_LIFECYCLE,
    SUPER_ADMIN_ROLE_CODE,
    can_approve,
    effectivity_contains,
    is_editable,
)

AUTHOR = uuid.uuid4()
REVIEWER = uuid.uuid4()


def principal(
    user_id: uuid.UUID,
    *,
    is_superuser: bool = False,
    roles: tuple[str, ...] = ("DESIGN_ENGINEER",),
) -> Principal:
    return Principal(
        user_id=user_id,
        company_id=uuid.uuid4(),
        email="tester@atcraft.test",
        full_name="Tester",
        is_superuser=is_superuser,
        permissions=frozenset({str(Permission.BOM_APPROVE)}),
        role_codes=roles,
    )


# -----------------------------------------------------------------------------
# The state graph
# -----------------------------------------------------------------------------


def test_the_state_graph_is_structurally_sound() -> None:
    # Catches an unreachable state or a non-terminal dead end.
    BOM_LIFECYCLE.validate()


def test_a_draft_can_be_submitted_or_abandoned() -> None:
    assert set(BOM_LIFECYCLE.allowed_targets(BomStatus.DRAFT)) == {
        BomStatus.PENDING_APPROVAL,
        BomStatus.OBSOLETE,
    }


def test_approval_cannot_skip_review() -> None:
    """A draft goes to a reviewer first: approval is the record of a second
    person having looked at it, so there is no direct DRAFT -> APPROVED edge."""
    with pytest.raises(InvalidStateTransitionError) as caught:
        BOM_LIFECYCLE.assert_transition(BomStatus.DRAFT, BomStatus.APPROVED)

    assert caught.value.details["current_status"] == "DRAFT"
    assert "PENDING_APPROVAL" in caught.value.details["allowed"]


def test_a_submitted_structure_can_be_approved_or_returned() -> None:
    assert set(BOM_LIFECYCLE.allowed_targets(BomStatus.PENDING_APPROVAL)) == {
        BomStatus.APPROVED,
        BomStatus.DRAFT,
        BomStatus.OBSOLETE,
    }


def test_an_approved_structure_can_only_go_obsolete() -> None:
    """Never back to draft: production orders and MRP runs already reference it,
    and a structure that changed under them is unreconcilable."""
    assert BOM_LIFECYCLE.allowed_targets(BomStatus.APPROVED) == [BomStatus.OBSOLETE]


def test_obsolete_is_terminal() -> None:
    assert BOM_LIFECYCLE.is_terminal(BomStatus.OBSOLETE)
    assert BOM_LIFECYCLE.allowed_targets(BomStatus.OBSOLETE) == []


def test_a_no_op_transition_is_refused() -> None:
    # A double-clicked "approve" must not write a second audit row claiming a
    # status change that did not happen.
    with pytest.raises(InvalidStateTransitionError):
        BOM_LIFECYCLE.assert_transition(BomStatus.APPROVED, BomStatus.APPROVED)


def test_approval_and_retirement_need_their_own_permissions() -> None:
    approve = BOM_LIFECYCLE.find(BomStatus.PENDING_APPROVAL, BomStatus.APPROVED)
    retire = BOM_LIFECYCLE.find(BomStatus.APPROVED, BomStatus.OBSOLETE)

    assert approve is not None and approve.permission is Permission.BOM_APPROVE
    assert retire is not None and retire.permission is Permission.BOM_OBSOLETE
    # Both release or withdraw a structure production depends on.
    assert approve.is_irreversible and retire.is_irreversible


def test_an_engineer_without_approval_rights_sees_no_approve_action() -> None:
    actions = BOM_LIFECYCLE.available_actions(
        BomStatus.PENDING_APPROVAL, [Permission.BOM_UPDATE, Permission.BOM_VIEW]
    )
    assert [t.target for t in actions] == []


def test_a_reviewer_sees_approve_and_return_but_not_retire() -> None:
    actions = BOM_LIFECYCLE.available_actions(BomStatus.PENDING_APPROVAL, [Permission.BOM_APPROVE])
    assert {t.target for t in actions} == {BomStatus.APPROVED, BomStatus.DRAFT}


def test_the_graph_renders_for_a_status_stepper() -> None:
    graph = BOM_LIFECYCLE.to_graph()
    assert graph["DRAFT"] == ["PENDING_APPROVAL", "OBSOLETE"]
    assert "APPROVED" in graph["PENDING_APPROVAL"]


# -----------------------------------------------------------------------------
# Editability
# -----------------------------------------------------------------------------


def test_only_a_draft_is_editable() -> None:
    assert is_editable(BomStatus.DRAFT) is True
    for status in (BomStatus.PENDING_APPROVAL, BomStatus.APPROVED, BomStatus.OBSOLETE):
        assert is_editable(status) is False


# -----------------------------------------------------------------------------
# Segregation of duties (docs/assumptions.md section 2)
# -----------------------------------------------------------------------------


def test_an_author_cannot_approve_their_own_structure() -> None:
    assert can_approve(principal(AUTHOR), AUTHOR) is False


def test_a_second_person_can_approve_it() -> None:
    assert can_approve(principal(REVIEWER), AUTHOR) is True


def test_a_superuser_may_approve_their_own_work() -> None:
    """The documented escape hatch: a one-person engineering team would
    otherwise be unable to release anything at all."""
    assert can_approve(principal(AUTHOR, is_superuser=True), AUTHOR) is True


def test_the_super_admin_role_carries_the_same_exemption() -> None:
    assert can_approve(principal(AUTHOR, roles=(SUPER_ADMIN_ROLE_CODE,)), AUTHOR) is True


def test_a_structure_with_no_recorded_author_can_be_approved() -> None:
    """Seeded or imported data has no creator; refusing it would leave those
    rows permanently unapprovable."""
    assert can_approve(principal(REVIEWER), None) is True


def test_holding_the_approve_permission_is_not_enough_on_its_own() -> None:
    # The author here holds bom.approve; the control is about identity, not
    # about the permission, and the two are checked separately.
    author = principal(AUTHOR)
    assert author.has(Permission.BOM_APPROVE) is True
    assert can_approve(author, AUTHOR) is False


# -----------------------------------------------------------------------------
# Effectivity
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """The two columns ``effectivity_contains`` reads."""

    effective_from: date
    effective_to: date | None


def window(start: date, end: date | None = None) -> Any:
    return cast(Any, Window(effective_from=start, effective_to=end))


def test_a_date_inside_an_open_ended_window_is_effective() -> None:
    bom = window(date(2026, 1, 1))
    assert effectivity_contains(bom, date(2026, 8, 31)) is True


def test_a_date_before_the_window_is_not_effective() -> None:
    bom = window(date(2026, 9, 1))
    assert effectivity_contains(bom, date(2026, 8, 31)) is False


def test_a_date_after_a_closed_window_is_not_effective() -> None:
    bom = window(date(2026, 1, 1), date(2026, 6, 30))
    assert effectivity_contains(bom, date(2026, 8, 31)) is False


def test_the_window_is_inclusive_at_both_ends() -> None:
    # Half-open would make the changeover day ambiguous, which is exactly the
    # day a planner asks about.
    bom = window(date(2026, 1, 1), date(2026, 6, 30))
    assert effectivity_contains(bom, date(2026, 1, 1)) is True
    assert effectivity_contains(bom, date(2026, 6, 30)) is True
