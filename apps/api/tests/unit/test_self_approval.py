"""Segregation of duties refuses in one shape, from one predicate.

The rule (docs/assumptions.md section 2) answered with three error contracts:
403 APPROVAL_REQUIRED from procurement, production and vendors, 403 FORBIDDEN
from BOM, routing, sales and the approval inbox, and 422 BUSINESS_RULE_VIOLATION
from allocation and inventory - so the toast read "Approval required", "Not
permitted" or "Could not complete" depending on the screen, in six wordings.
Five modules carried a word-for-word copy of ``can_approve``. Now one predicate
decides and :class:`SelfApprovalError` refuses.

The guards are functions over source text, each with a self-test that feeds it
the violation it exists to catch.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest

from src.common.approvals import SUPER_ADMIN_ROLE_CODE, can_approve
from src.core.deps import Principal
from src.core.errors import AuthorizationError, ErrorCode, SelfApprovalError
from src.core.messages import self_approval_refused
from src.modules.bom import service as bom
from src.modules.procurement import service as procurement
from src.modules.production import service as production
from src.modules.routing import service as routing
from src.modules.sales import service as sales

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"
AUTHOR = uuid.uuid4()


def _principal(user_id: uuid.UUID, *, roles: tuple[str, ...] = (), superuser: bool = False):
    return Principal(
        user_id=user_id,
        company_id=uuid.uuid4(),
        email="approver@atcraft.example",
        full_name="Approver",
        permissions=frozenset(),
        role_codes=roles,
        is_superuser=superuser,
    )


# --- the refusal -------------------------------------------------------------------


def test_the_refusal_is_one_status_one_code_and_one_sentence() -> None:
    refused = SelfApprovalError("A sales order", details={"order_id": "x"})

    assert isinstance(refused, AuthorizationError)
    assert (refused.status_code, refused.code) == (403, ErrorCode.APPROVAL_REQUIRED)
    assert refused.message == (
        "A sales order must be approved by someone other than the person who raised it."
    )
    assert refused.details == {"order_id": "x"}


def test_the_sentence_names_the_decision_and_how_the_record_began() -> None:
    assert self_approval_refused("An internal issue", decision="approved or rejected") == (
        "An internal issue must be approved or rejected by someone other than the person "
        "who raised it."
    )
    assert self_approval_refused("A vendor", origin="created") == (
        "A vendor must be approved by someone other than the person who created it."
    )


# --- the predicate -----------------------------------------------------------------


def test_the_author_may_not_approve_and_a_second_person_may() -> None:
    assert can_approve(_principal(AUTHOR), AUTHOR) is False
    assert can_approve(_principal(uuid.uuid4()), AUTHOR) is True
    assert can_approve(_principal(AUTHOR), None) is True


def test_a_superuser_or_a_super_admin_may_approve_their_own() -> None:
    assert can_approve(_principal(AUTHOR, superuser=True), AUTHOR) is True
    assert can_approve(_principal(AUTHOR, roles=(SUPER_ADMIN_ROLE_CODE,)), AUTHOR) is True


@pytest.mark.parametrize("module", [bom, procurement, production, routing, sales])
def test_every_approving_module_applies_the_one_predicate(module) -> None:
    assert module.can_approve is can_approve


# --- the guards ----------------------------------------------------------------------

_REFUSAL = re.compile(
    r"\b(?:approved|rejected|decided)\b[^.]*\bby (?:someone other|the person who)", re.I
)


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def can_approve_definitions(source: str) -> int:
    return sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "can_approve"
        for node in ast.walk(ast.parse(source))
    )


def hand_written_self_approval_refusals(source: str) -> list[str]:
    """``line N`` for each raise that words the rule itself instead of SelfApprovalError."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        raised = node.exc.func
        if isinstance(raised, ast.Name) and raised.id == "SelfApprovalError":
            continue
        text = " ".join(
            part.value
            for argument in node.exc.args[:1]
            for part in ast.walk(argument)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        if _REFUSAL.search(text):
            found.append(node.lineno)
    return [f"line {line}" for line in sorted(found)]


def test_can_approve_is_defined_once() -> None:
    owners = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if can_approve_definitions(path.read_text(encoding="utf-8"))
    ]

    assert owners == ["common/approvals.py"], (
        f"can_approve is defined in {owners}; import it from src.common.approvals."
    )


def test_every_self_approval_refusal_is_a_self_approval_error() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _python_files()
        for hit in hand_written_self_approval_refusals(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        "A self-approval refusal is worded by hand:\n"
        + "\n".join(failures)
        + "\n\nRaise SelfApprovalError(subject) so it answers 403 APPROVAL_REQUIRED in the "
        "shared sentence."
    )


def test_the_refusal_rule_flags_the_shapes_that_shipped() -> None:
    source = "\n".join(
        [
            "def approve(order):",
            "    raise AuthorizationError(",
            "        'A routing must be approved by someone other than its author.',",
            "    )",
            "    raise BusinessRuleError(",
            "        'An internal issue must be approved or rejected by someone other than the '",
            "        'person who raised it.',",
            "    )",
            "    raise AuthorizationError('A PO cannot be approved by the person who raised it.')",
            "    raise SelfApprovalError('A routing')",
            "    raise BusinessRuleError('This action requires approval before it completes.')",
        ]
    )

    assert hand_written_self_approval_refusals(source) == ["line 2", "line 5", "line 9"]


def test_the_definition_rule_counts_each_copy() -> None:
    copy = "def can_approve(principal, created_by_id):\n    return True\n"

    assert can_approve_definitions(copy + "\n" + copy) == 2
    assert can_approve_definitions("from src.common.approvals import can_approve\n") == 0
