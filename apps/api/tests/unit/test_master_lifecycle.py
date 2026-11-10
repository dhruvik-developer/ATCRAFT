"""The one master lifecycle, and the rules that keep its copies from coming back.

Seven modules each declared the ACTIVE / INACTIVE / ARCHIVED enum, the function
deriving it from ``is_active`` and ``deleted_at``, and the machine. The copies had
drifted: the work-centre and storage-location machines declared an
ARCHIVED -> INACTIVE "Restore" edge that no route or service method performs,
which also kept ARCHIVED non-terminal in both. ``src/common/master_lifecycle.py``
is now the one definition, and these tests pin which masters may come back.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.modules
from src.common.master_lifecycle import MasterLifecycleState, master_lifecycle, master_state
from src.core.permissions import Permission
from src.core.state_machine import StateMachine
from tests.route_support import module_routes

pytestmark = pytest.mark.unit

ACTIVE, INACTIVE, ARCHIVED = (
    MasterLifecycleState.ACTIVE,
    MasterLifecycleState.INACTIVE,
    MasterLifecycleState.ARCHIVED,
)
SRC = Path(__file__).resolve().parents[2] / "src"
SHARED = "common/master_lifecycle.py"

#: Enums with exactly the three lifecycle members that are not the shared one,
#: each with the reason it stays.
LIFECYCLE_ENUM_ALLOWED: dict[str, str] = {
    "modules/products/models.py::ItemLifecycleState": (
        "published in the API contract under this name, as the item's lifecycle_state"
    ),
    "modules/dealer_portal/models.py::DealerBannerStatus": (
        "a stored status column with its own CHECK constraint, not a derived master state"
    ),
}

#: The masters that have a restore route, and therefore a Restore edge.
RESTORABLE_MASTERS = frozenset({"Customer", "Transporter", "Warehouse"})


# -----------------------------------------------------------------------------
# The factory
# -----------------------------------------------------------------------------


def _edges(machine: StateMachine[MasterLifecycleState]) -> set[tuple[str, str]]:
    return {(str(t.source), str(t.target)) for t in machine.transitions}


def test_a_master_without_a_restore_route_retires_for_good() -> None:
    machine = master_lifecycle("Plant", Permission.PLANT_MANAGE)

    machine.validate()
    assert _edges(machine) == {
        ("ACTIVE", "INACTIVE"),
        ("INACTIVE", "ACTIVE"),
        ("ACTIVE", "ARCHIVED"),
        ("INACTIVE", "ARCHIVED"),
    }
    assert machine.is_terminal(ARCHIVED)
    assert machine.allowed_targets(ARCHIVED) == []


def test_a_restorable_master_comes_back_inactive_never_active() -> None:
    machine = master_lifecycle("Warehouse", Permission.WAREHOUSE_MANAGE, restorable=True)

    machine.validate()
    assert machine.allowed_targets(ARCHIVED) == [INACTIVE]
    assert not machine.is_terminal(ARCHIVED)
    restore = machine.find(ARCHIVED, INACTIVE)
    assert restore is not None
    assert restore.label == "Restore"
    assert restore.permission is Permission.WAREHOUSE_MANAGE


def test_retirement_takes_its_own_right_when_one_is_named() -> None:
    machine = master_lifecycle(
        "Customer", Permission.CUSTOMER_UPDATE, retire=Permission.CUSTOMER_DELETE
    )

    for source in (ACTIVE, INACTIVE):
        retire = machine.find(source, ARCHIVED)
        assert retire is not None
        assert retire.permission is Permission.CUSTOMER_DELETE
        assert retire.is_irreversible
    deactivate = machine.find(ACTIVE, INACTIVE)
    assert deactivate is not None
    assert deactivate.permission is Permission.CUSTOMER_UPDATE


def test_the_state_is_derived_from_the_flags_and_retirement_wins() -> None:
    retired = datetime(2026, 11, 1, tzinfo=UTC)

    assert master_state(SimpleNamespace(is_active=True, deleted_at=None)) is ACTIVE
    assert master_state(SimpleNamespace(is_active=False, deleted_at=None)) is INACTIVE
    assert master_state(SimpleNamespace(is_active=True, deleted_at=retired)) is ARCHIVED


# -----------------------------------------------------------------------------
# Every master machine is built from it, and only three may come back
# -----------------------------------------------------------------------------


def _machines() -> dict[str, StateMachine[object]]:
    found: dict[str, StateMachine[object]] = {}
    for info in pkgutil.iter_modules(src.modules.__path__):
        name = f"src.modules.{info.name}.service"
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        for attribute, value in vars(module).items():
            if isinstance(value, StateMachine):
                found[f"{name}.{attribute}"] = value
    return found


def _master_machines() -> dict[str, StateMachine[object]]:
    return {
        name: machine
        for name, machine in _machines().items()
        if machine.initial is MasterLifecycleState.ACTIVE
    }


def test_the_scan_finds_the_master_machines() -> None:
    entities = {machine.entity for machine in _master_machines().values()}
    assert {"Branch", "Work centre", "Storage location", "User", "Transporter"} <= entities


def test_restore_is_declared_exactly_for_the_masters_that_have_a_restore_route() -> None:
    restorable = {
        machine.entity
        for machine in _master_machines().values()
        if not machine.is_terminal(MasterLifecycleState.ARCHIVED)
    }
    assert restorable == RESTORABLE_MASTERS


def _restore_paths() -> set[str]:
    return {mounted.path for mounted in module_routes() if mounted.path.endswith("/restore")}


def restore_route_for(entity: str, paths: set[str]) -> bool:
    """Whether a ``.../{<entity>_id}/restore`` route exists for the entity."""
    parameter = "{" + re.sub(r"\W+", "_", entity.strip().lower()) + "_id}"
    return any(path.endswith(f"/{parameter}/restore") for path in paths)


def test_every_restore_edge_has_a_route_behind_it() -> None:
    paths = _restore_paths()
    dead = sorted(
        f"{name} ({machine.entity})"
        for name, machine in _machines().items()
        if any(t.label == "Restore" for t in machine.transitions)
        and not restore_route_for(machine.entity, paths)
    )
    assert dead == [], f"a Restore edge with no restore route behind it: {dead}"


def test_the_route_rule_rejects_a_restore_nothing_performs() -> None:
    paths = {"/warehouses/{warehouse_id}/restore", "/customers/{customer_id}/restore"}
    assert restore_route_for("Warehouse", paths)
    assert not restore_route_for("Storage location", paths)
    assert not restore_route_for("Work centre", paths)


# -----------------------------------------------------------------------------
# No module declares the enum again
# -----------------------------------------------------------------------------


def lifecycle_enums(source: str) -> list[str]:
    """Enum classes whose members are exactly ACTIVE, INACTIVE and ARCHIVED."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            (isinstance(base, ast.Name) and base.id.endswith("Enum"))
            or (isinstance(base, ast.Attribute) and base.attr.endswith("Enum"))
            for base in node.bases
        ):
            continue
        members = {
            target.id
            for statement in node.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        if members == {"ACTIVE", "INACTIVE", "ARCHIVED"}:
            found.append(node.name)
    return found


def test_no_module_declares_its_own_master_lifecycle_enum() -> None:
    copies = {
        f"{relative}::{name}"
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
        and (relative := path.relative_to(SRC).as_posix()) != SHARED
        for name in lifecycle_enums(path.read_text(encoding="utf-8"))
    }
    unexplained = sorted(copies - set(LIFECYCLE_ENUM_ALLOWED))
    assert unexplained == [], (
        "Import MasterLifecycleState and master_lifecycle from src.common.master_lifecycle "
        f"rather than declaring a copy: {unexplained}"
    )
    assert set(LIFECYCLE_ENUM_ALLOWED) <= copies, "remove allowances that cover nothing"


def test_the_enum_rule_rejects_a_copy_and_accepts_a_wider_vocabulary() -> None:
    source = (
        "class TransporterState(StrEnum):\n"
        "    ACTIVE = 'ACTIVE'\n"
        "    INACTIVE = 'INACTIVE'\n"
        "    ARCHIVED = 'ARCHIVED'\n"
        "class VendorState(StrEnum):\n"
        "    ACTIVE = 'ACTIVE'\n"
        "    INACTIVE = 'INACTIVE'\n"
        "    ARCHIVED = 'ARCHIVED'\n"
        "    SUSPENDED = 'SUSPENDED'\n"
    )
    assert lifecycle_enums(source) == ["TransporterState"]
