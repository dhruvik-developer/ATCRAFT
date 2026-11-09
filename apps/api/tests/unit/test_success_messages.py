"""A route that changes something says what it changed, in the shared shape.

``docs/api.md`` (Message conventions): a success title names its subject and a
past-tense outcome - ``Purchase order created`` - with no "successfully", and a
route builds it with :func:`src.core.messages.success_message`. Two drifts
shipped past review:

* eleven POST, PATCH and DELETE handlers passed ``ApiResponse.ok`` no message
  at all, so creating a courier account, removing a push subscription or
  reporting job-work progress all answered "Operation completed successfully" -
  the envelope's default, which names nothing;
* the promotions router spelled twelve literals "Promotion ... successfully",
  for banners and offers alike.

Both rules are functions over source text, and each has a self-test that feeds
it the violation it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.messages import plural, success_message

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

_MUTATING = frozenset({"post", "put", "patch", "delete"})


def _router_files() -> list[Path]:
    return sorted(path for path in (SRC / "modules").glob("*/*.py") if "router" in path.stem)


def _is_mutating_handler(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in _MUTATING
        for decorator in node.decorator_list
    )


def _envelope_ok_calls(handler: ast.AST) -> list[ast.Call]:
    """Every ``ApiResponse.ok(...)`` / ``ApiResponse[T].ok(...)`` inside ``handler``."""
    return [
        call
        for call in ast.walk(handler)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "ok"
        and ast.unparse(call.func.value).startswith("ApiResponse")
    ]


def _message_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "message":
            return keyword.value
    return call.args[1] if len(call.args) >= 2 else None


def _literal_text(node: ast.expr) -> str:
    """The constant parts of a str literal or f-string; empty for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def unnamed_outcomes(source: str) -> list[str]:
    """``line N handler`` for each mutating handler answering the default message."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not _is_mutating_handler(node):
            continue
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        found.extend(
            f"line {call.lineno} {node.name}"
            for call in _envelope_ok_calls(node)
            if _message_argument(call) is None
        )
    return sorted(found, key=lambda entry: int(entry.split()[1]))


def successfully_messages(source: str) -> list[str]:
    """``line N`` for each literal message argument that says "successfully"."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        candidates = [keyword.value for keyword in node.keywords if keyword.arg == "message"]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "ok":
            candidates.extend(node.args[1:2])
        found.extend(
            candidate.lineno
            for candidate in candidates
            if "successfully" in _literal_text(candidate).lower()
        )
    return [f"line {line}" for line in sorted(found)]


def test_every_mutating_route_names_its_outcome() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _router_files()
        for hit in unnamed_outcomes(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        f"{len(failures)} mutating route(s) answer the envelope's default message:\n"
        + "\n".join(failures)
        + "\n\nPass success_message(subject, outcome) - 'Courier account created' - "
        "so the response says what changed."
    )


def test_no_route_message_says_successfully() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _router_files()
        for hit in successfully_messages(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        f"{len(failures)} route message(s) say 'successfully':\n"
        + "\n".join(failures)
        + "\n\nName the subject and a past-tense outcome instead: "
        "success_message('Banner', 'activated')."
    )


def test_the_outcome_rule_flags_a_mutating_route_with_no_message() -> None:
    source = "\n".join(
        [
            "@router.post('/accounts')",
            "async def create_account(payload, session):",
            "    return ApiResponse.ok(await service.create(payload))",
            "@router.patch('/accounts/{id}')",
            "async def update_account(payload, session):",
            "    return ApiResponse[AccountRead].ok(row, success_message('Account', 'updated'))",
            "@router.delete('/accounts/{id}')",
            "async def remove_account(session):",
            "    return ApiResponse.ok(None, message=REMOVED)",
            "@router.get('/accounts')",
            "async def list_accounts(session):",
            "    return ApiResponse.ok(rows)",
            "@router.post('/devices')",
            "async def add_device(session):",
            "    return ApiResponse[DeviceRead].ok(",
            "        await service.add()",
            "    )",
        ]
    )

    # A read may answer the default; a write that names its outcome passes either way.
    assert unnamed_outcomes(source) == ["line 3 create_account", "line 15 add_device"]


def test_the_wording_rule_flags_successfully_in_any_literal_message() -> None:
    source = "\n".join(
        [
            "ApiResponse.ok(row, 'Promotion activated successfully')",
            "ApiResponse[Read].ok(row, f'Order {code} created Successfully')",
            "AcceptedJobResponse.create(job_id=j, status_url=u, message='Queued successfully')",
            "ApiResponse.ok(row, success_message('Banner', 'activated'))",
            "ApiResponse.ok(row, f'Order {code} created')",
            "# Promotion activated successfully",
        ]
    )

    assert successfully_messages(source) == ["line 1", "line 2", "line 3"]


def test_the_shared_builders_read_as_titles() -> None:
    assert success_message("Banner", "activated") == "Banner activated"
    assert success_message("Courier connection", "verified") == "Courier connection verified"
    assert plural(1, "demand line") == "1 demand line"
    assert plural(3, "demand line") == "3 demand lines"
