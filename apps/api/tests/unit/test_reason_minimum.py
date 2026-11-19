"""An audited reason's floor is one constant, and the refusal states it from there.

Two refusals spelled the floor out - "at least three characters" - while the
fields they guard were bound by a literal ``min_length=3`` beside them. Raise the
floor and both sentences would keep promising the old one while the API refused
on the new. ``REASON_MIN_LENGTH`` now bounds the fields and ``REASON_TOO_SHORT``
interpolates it: the frontend's ``errors.reasonTooShort``, word for word.

Only two schemas took the constant at first; about forty reason bounds across
the other modules, and two ``len(reason) < 3`` checks in procurement, still wrote
the floor as a number. :func:`unshared_reason_floors` refuses that shape, so the
floor moves in one place or not at all.

A number was not the only way round it. The approvals reject path checked
``len(note) < MIN_REJECTION_NOTE_LENGTH`` - a constant of its own, on a field
called ``note`` - and refused in a sentence of its own, so raising the shared
floor would have left it accepting what every other reason refused. A
``len()`` check on a reason or a note is now held to the shared constant by
name, whatever it is compared with instead.
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core import errors
from src.core.constants import REASON_MIN_LENGTH
from src.core.deps import Principal, TenantContext
from src.core.messages import REASON_TOO_SHORT
from src.modules.audit.query_service import ApprovalService
from src.modules.audit.schemas import ApprovalDecisionRequest
from src.modules.dealer_portal.promotion_schemas import PromotionRetire
from src.modules.inventory.schemas import RejectionRequest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"


def test_the_sentence_states_the_floor_from_the_constant() -> None:
    assert f"Record at least {REASON_MIN_LENGTH} characters of reason." == REASON_TOO_SHORT
    assert "three" not in REASON_TOO_SHORT


@pytest.mark.parametrize(
    "schema,field",
    [(PromotionRetire, "reason"), (RejectionRequest, "note")],
)
def test_a_reason_that_is_padding_is_refused_in_the_shared_sentence(schema, field) -> None:
    # Long enough for the field's own bound, one character once the padding is gone.
    padded = "a".ljust(REASON_MIN_LENGTH)
    with pytest.raises(ValidationError) as error:
        schema.model_validate({field: padded})

    [problem] = error.value.errors()
    assert problem["loc"] == (field,)
    assert str(problem["ctx"]["error"]) == REASON_TOO_SHORT


@pytest.mark.parametrize(
    "schema,field",
    [(PromotionRetire, "reason"), (RejectionRequest, "note")],
)
def test_a_reason_at_the_floor_is_kept(schema, field) -> None:
    accepted = schema.model_validate({field: "x" * REASON_MIN_LENGTH})

    assert getattr(accepted, field) == "x" * REASON_MIN_LENGTH


@pytest.mark.parametrize("note", [None, "x" * (REASON_MIN_LENGTH - 1)])
async def test_a_rejection_short_of_the_floor_is_refused_in_the_shared_sentence(
    note: str | None,
) -> None:
    """The approvals reject path holds its note to the shared floor, before any read.

    No session is given: the note is checked before the request is looked up.
    """
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    service = ApprovalService(
        cast(AsyncSession, None),
        TenantContext(company_id=company_id, user_id=user_id),
        Principal(
            user_id=user_id,
            company_id=company_id,
            email="approver@atcraft-qa.com",
            full_name="Approver",
            is_superuser=False,
            permissions=frozenset(),
        ),
    )

    with pytest.raises(errors.ValidationError) as refused:
        await service.reject(uuid.uuid4(), ApprovalDecisionRequest(note=note))

    assert refused.value.message == REASON_TOO_SHORT
    assert refused.value.details == {"field": "note", "min_length": REASON_MIN_LENGTH}


# -----------------------------------------------------------------------------
# No reason floor is written as a number
# -----------------------------------------------------------------------------

_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _names_a_reason(name: str) -> bool:
    """``reason``, ``rejection_reason``, ``Reason``, ``ReasonQuery`` - not ``reason_code``.

    A reason code is a vocabulary key with a width of its own; the floor is for
    the free text a person writes to justify the change.
    """
    words = {word.lower() for word in _WORDS.findall(name)}
    return "reason" in words and "code" not in words


def _named_parts(tree: ast.AST) -> Iterator[tuple[str, ast.AST]]:
    """Each field, alias and parameter, by name, with the expression that bounds it."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            positional = [*arguments.posonlyargs, *arguments.args]
            for arg in [*positional, *arguments.kwonlyargs]:
                if arg.annotation is not None:
                    yield arg.arg, arg.annotation
            defaulted = positional[len(positional) - len(arguments.defaults) :]
            for arg, default in zip(defaulted, arguments.defaults, strict=True):
                yield arg.arg, default
            for arg, kw_default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
                if kw_default is not None:
                    yield arg.arg, kw_default


def _literal_min_lengths(node: ast.AST) -> Iterator[int]:
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            for keyword in call.keywords:
                value = keyword.value
                if (
                    keyword.arg == "min_length"
                    and isinstance(value, ast.Constant)
                    and type(value.value) is int
                ):
                    yield value.lineno


#: The one name a reason's floor is read from, bare or module-qualified.
SHARED_FLOOR = "REASON_MIN_LENGTH"


def _names_a_justification(name: str) -> bool:
    """A reason, or a decision's ``note`` - not a document's free-text ``notes``.

    A rejection's note is a reason by another name, and the one ``len()`` floor
    that escaped :func:`_names_a_reason` was checked on a field called ``note``.
    """
    words = {word.lower() for word in _WORDS.findall(name)}
    return _names_a_reason(name) or "note" in words


def _reads_a_justification(node: ast.AST) -> bool:
    return any(
        (isinstance(part, ast.Name) and _names_a_justification(part.id))
        or (isinstance(part, ast.Attribute) and _names_a_justification(part.attr))
        for part in ast.walk(node)
    )


def _is_the_shared_floor(bound: ast.expr) -> bool:
    return (isinstance(bound, ast.Name) and bound.id == SHARED_FLOOR) or (
        isinstance(bound, ast.Attribute) and bound.attr == SHARED_FLOOR
    )


def _unshared_length_floors(tree: ast.AST) -> Iterator[int]:
    """``len(<reason or note>) < <anything but REASON_MIN_LENGTH>``, in a validator or a service.

    A number, or a constant the module keeps for itself: either is a floor that
    stays put when the shared one moves.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "len"
            and _reads_a_justification(node.left)
            and any(isinstance(op, (ast.Lt, ast.LtE)) for op in node.ops)
            and not all(_is_the_shared_floor(bound) for bound in node.comparators)
        ):
            yield node.lineno


def unshared_reason_floors(source: str) -> list[str]:
    """``line N`` for each reason whose floor is not ``REASON_MIN_LENGTH``."""
    tree = ast.parse(source)
    lines = {
        line
        for name, part in _named_parts(tree)
        if _names_a_reason(name)
        for line in _literal_min_lengths(part)
    }
    lines.update(_unshared_length_floors(tree))
    return [f"line {line}" for line in sorted(lines)]


def test_every_reason_floor_is_the_shared_constant() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
        for hit in unshared_reason_floors(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        f"{len(failures)} reason floor(s) are not REASON_MIN_LENGTH:\n"
        + "\n".join(failures)
        + "\n\nBound a reason with src.core.constants.REASON_MIN_LENGTH - "
        "Field(min_length=REASON_MIN_LENGTH), len(reason) < REASON_MIN_LENGTH - so the "
        "floor, the frontend's copy of it and REASON_TOO_SHORT move together."
    )


def test_the_floor_rule_flags_numbers_and_other_names_and_passes_the_constant() -> None:
    source = "\n".join(
        [
            "Reason = Annotated[str, Field(min_length=3, max_length=255)]",
            "ReasonQuery = Annotated[",
            "    str, Query(min_length=3, max_length=255, description='Why it goes')",
            "]",
            "Kept = Annotated[str, Field(min_length=REASON_MIN_LENGTH, max_length=255)]",
            "Code = Annotated[str, Field(min_length=2, max_length=64)]",
            "class Cancel(BaseModel):",
            "    reason: str = Field(min_length=3, max_length=500)",
            "    rejection_reason: Annotated[str, Field(min_length=4)] | None = None",
            "    reason_code: str = Field(min_length=2, max_length=32)",
            "    note: str = Field(min_length=REASON_MIN_LENGTH)",
            "    def check(self):",
            "        if self.confirm and len(self.reason or '') < 3:",
            "            raise ValueError('Give a reason.')",
            "        if len(self.reason) > 255:",
            "            raise ValueError('Too long.')",
            "        if len(self.reason.strip()) < REASON_MIN_LENGTH:",
            "            raise ValueError(REASON_TOO_SHORT)",
            "async def delete(reason: Annotated[str, Query(min_length=3)]): ...",
            "async def retire(reason: str = Query(min_length=3)): ...",
            "async def purge(*, reason: str = Query(min_length=3)): ...",
            "async def search(code: Annotated[str, Query(min_length=2)]): ...",
            "def decide(note, reason, notes):",
            "    if note is None or len(note) < MIN_REJECTION_NOTE_LENGTH:",
            "        raise ValidationError('A rejection must state a reason.')",
            "    if len(reason.strip()) <= MIN_CANCEL_REASON:",
            "        raise ValueError('Give a reason.')",
            "    if len(note) < REASON_MIN_LENGTH or len(reason) < core.REASON_MIN_LENGTH:",
            "        raise ValueError(REASON_TOO_SHORT)",
            "    if len(notes) < 1:",
            "        raise ValueError('Notes cannot be blank.')",
        ]
    )

    assert unshared_reason_floors(source) == [
        "line 1",
        "line 3",
        "line 8",
        "line 9",
        "line 13",
        "line 19",
        "line 20",
        "line 21",
        "line 24",
        "line 26",
    ]
