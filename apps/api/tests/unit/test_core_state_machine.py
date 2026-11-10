"""``StateMachine.assert_transition`` with a pinned source, and who may refuse a transition.

Seven services - customer, vendor, transporter and warehouse restore, the
physical count's recount, sales order confirmation and vendor invoice
submission - each wrote the same refusal by hand: "assert the source as well as
the edge", because the machine has a second edge into the same target and the
edge alone would let one operation perform another. ``source=`` says it once;
the guard below keeps the next one from writing the error out again.
"""

from __future__ import annotations

import ast
from enum import StrEnum
from pathlib import Path

import pytest

from src.core.errors import ErrorCode, InvalidStateTransitionError
from src.core.state_machine import StateMachine, Transition

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"


class _State(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


#: The master lifecycle's shape: restore and deactivate both end INACTIVE.
_MACHINE: StateMachine[_State] = StateMachine(
    entity="Warehouse",
    initial=_State.ACTIVE,
    transitions=(
        Transition(_State.ACTIVE, _State.INACTIVE, label="Deactivate"),
        Transition(_State.INACTIVE, _State.ACTIVE, label="Activate"),
        Transition(_State.ACTIVE, _State.ARCHIVED, label="Retire"),
        Transition(_State.ARCHIVED, _State.INACTIVE, label="Restore"),
    ),
)


def test_the_pinned_source_admits_its_own_edge() -> None:
    transition = _MACHINE.assert_transition(
        _State.ARCHIVED, _State.INACTIVE, source=_State.ARCHIVED, requested="RESTORED"
    )

    assert transition.label == "Restore"


def test_the_pinned_source_refuses_the_other_edge_into_the_same_target() -> None:
    # ACTIVE -> INACTIVE is a real edge, so without ``source`` this would pass
    # and "restore" would quietly deactivate a live record.
    assert _MACHINE.can_transition(_State.ACTIVE, _State.INACTIVE)

    with pytest.raises(InvalidStateTransitionError) as excinfo:
        _MACHINE.assert_transition(
            _State.ACTIVE, _State.INACTIVE, source=_State.ARCHIVED, requested="RESTORED"
        )

    error = excinfo.value
    assert error.code is ErrorCode.INVALID_STATE_TRANSITION
    assert error.status_code == 422
    assert error.message == "Warehouse cannot move from 'ACTIVE' to 'RESTORED'."
    assert error.details == {
        "current_status": "ACTIVE",
        "requested_status": "RESTORED",
        "allowed": ["INACTIVE", "ARCHIVED"],
    }


def test_the_refusal_names_the_target_when_no_operation_is_named() -> None:
    with pytest.raises(InvalidStateTransitionError) as excinfo:
        _MACHINE.assert_transition(_State.ACTIVE, _State.INACTIVE, source=_State.INACTIVE)

    assert excinfo.value.details["requested_status"] == "INACTIVE"


def test_without_a_source_only_the_edge_is_checked() -> None:
    assert _MACHINE.assert_transition(_State.ACTIVE, _State.INACTIVE).label == "Deactivate"

    with pytest.raises(InvalidStateTransitionError) as excinfo:
        _MACHINE.assert_transition(_State.INACTIVE, _State.ARCHIVED)

    assert excinfo.value.details == {
        "current_status": "INACTIVE",
        "requested_status": "ARCHIVED",
        "allowed": ["ACTIVE"],
    }


# --- the guard ------------------------------------------------------------------

#: The one module that words a refused transition.
_OWNER = "core/state_machine.py"

#: Functions elsewhere that raise it themselves, each with the reason.
_ALLOWED: dict[str, str] = {
    "modules/quality/service.py:_assert_decidable": (
        "asks whether any decided status is reachable, not whether one edge exists"
    ),
}


def transition_errors_raised(source: str) -> list[str]:
    """The enclosing function of every ``InvalidStateTransitionError(...)`` call."""
    found: list[str] = []

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else function
            )
            if isinstance(child, ast.Call):
                called = child.func
                label = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
                if label == "InvalidStateTransitionError":
                    found.append(name)
            visit(child, name)

    visit(ast.parse(source), "<module>")
    return found


def test_the_guard_finds_a_hand_written_refusal() -> None:
    source = (
        "def restore(current):\n"
        "    if current is not ARCHIVED:\n"
        "        raise InvalidStateTransitionError(\n"
        "            entity='Vendor', current=str(current), requested='RESTORED', allowed=[]\n"
        "        )\n"
    )

    assert transition_errors_raised(source) == ["restore"]


def test_only_the_state_machine_words_a_refused_transition() -> None:
    offenders = sorted(
        f"{relative}:{function}"
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
        and (relative := path.relative_to(SRC).as_posix()) != _OWNER
        for function in transition_errors_raised(path.read_text(encoding="utf-8"))
        if f"{relative}:{function}" not in _ALLOWED
    )

    assert not offenders, (
        "Refuse a transition through StateMachine.assert_transition - pass source= "
        "when the operation leaves from one state only - rather than building "
        f"InvalidStateTransitionError by hand: {offenders}"
    )


def test_every_exemption_is_still_needed() -> None:
    raised = {
        f"{path.relative_to(SRC).as_posix()}:{function}"
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
        for function in transition_errors_raised(path.read_text(encoding="utf-8"))
    }

    assert set(_ALLOWED) <= raised
