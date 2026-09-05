"""Every state machine names its document the way a person would.

``InvalidStateTransitionError`` renders ``"{entity} cannot move from A to B"``,
and the same record's not-found message comes from ``get_or_404``. Half the
machines used to carry the model's class name ("StockTransfer", "PhysicalCount",
"Bom", "user_session") while the module's own messages said "Stock transfer",
so one record answered with two spellings. This scans every service module for
its machines and refuses an identifier where a name should be.
"""

from __future__ import annotations

import importlib
import pkgutil
import re

import pytest

import src.modules
from src.core.state_machine import StateMachine

pytestmark = pytest.mark.unit

#: A first word that is either an acronym ("BOM", "QC") or capitalised
#: ("Stock"), every following word lower case - the shape the not-found labels
#: take, and one no CamelCase class name, snake_case identifier or lower-case
#: word can match.
_HUMAN_NAME = re.compile(r"^(?:[A-Z]+|[A-Z][a-z]+)( [a-z0-9]+)*$")

#: Service modules that do not follow the ``<package>.service`` convention.
_EXTRA_MODULES = ("src.modules.audit.query_service", "src.modules.procurement.service_rules")


def _machines() -> dict[str, StateMachine[object]]:
    found: dict[str, StateMachine[object]] = {}
    names = [
        f"src.modules.{info.name}.service" for info in pkgutil.iter_modules(src.modules.__path__)
    ]
    for name in [*names, *_EXTRA_MODULES]:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        for attribute, value in vars(module).items():
            if isinstance(value, StateMachine):
                found[f"{name}.{attribute}"] = value
    return found


def test_the_scan_finds_the_machines() -> None:
    assert len(_machines()) >= 30


def test_every_machine_names_its_document_for_a_person() -> None:
    offenders = {
        name: machine.entity
        for name, machine in _machines().items()
        if not _HUMAN_NAME.match(machine.entity)
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "bad", ["StockTransfer", "PhysicalCount", "user_session", "document", "WorkCenter"]
)
def test_the_rule_rejects_the_spellings_that_shipped(bad: str) -> None:
    # "Bom" is not here: by shape it is a word, and only the module knows it is
    # an acronym - which is why its machine and repository spell it "BOM".
    assert not _HUMAN_NAME.match(bad)


@pytest.mark.parametrize(
    "good", ["Stock transfer", "BOM", "QC inspection", "Work centre", "Session"]
)
def test_the_rule_accepts_the_spellings_the_messages_use(good: str) -> None:
    assert _HUMAN_NAME.match(good)
