"""Sessions end through one repository, in one vocabulary.

``auth`` and ``users`` each defined a ``UserSessionRepository`` over
``user_sessions`` and each burned password-reset grants with its own UPDATE.
The copies had already drifted - one truncated ``revoked_reason`` to the column
width, the other wrote it as given - and the reasons were constants in one
module and string literals in the other. A change to how a session ends, or
how a recovery link dies, both security-relevant, had to be made twice.

``users`` owns both tables (docs/database.md section 2), so the one
``UserSessionRepository`` and the one grant invalidation live there and auth
imports them; the reasons are ``SessionRevocationReason``.
"""

from __future__ import annotations

import ast
from typing import get_type_hints

import pytest

from src.core.config import API_ROOT
from src.modules.auth import repository as auth_repository
from src.modules.auth import service as auth_service
from src.modules.users import repository as users_repository
from src.modules.users.models import SessionRevocationReason, UserSession

pytestmark = pytest.mark.unit

SOURCE = API_ROOT / "src"

#: Every value rows already carry. A security report groups by these strings,
#: so a member may be renamed but its value never changed.
PERSISTED_VALUES = frozenset(
    {
        "logout",
        "logout_all",
        "rotated",
        "refresh_replay",
        "password_changed",
        "password_reset",
        "revoked_by_user",
        "account_locked",
        "mfa_enabled",
        "mfa_disabled",
        "password_changed_by_admin",
        "recovery_email_changed_by_admin",
        "user_deactivated",
        "user_retired",
    }
)


def test_every_reason_fits_the_column() -> None:
    width = UserSession.__table__.c.revoked_reason.type.length
    assert width == 64
    assert [reason for reason in SessionRevocationReason if len(reason) > width] == []


def test_no_value_a_stored_row_carries_has_changed() -> None:
    assert {reason.value for reason in SessionRevocationReason} == PERSISTED_VALUES


@pytest.mark.parametrize(
    "method", ["revoke", "revoke_all_for_user", "revoke_all_for_user_with_ids", "revoke_chain"]
)
def test_every_way_to_end_a_session_takes_the_vocabulary(method: str) -> None:
    """mypy refuses a bare string at every call site - while this annotation holds."""
    hints = get_type_hints(getattr(users_repository.UserSessionRepository, method))
    assert hints["reason"] is SessionRevocationReason


def session_repositories(sources: dict[str, str]) -> list[str]:
    """Repository classes that declare ``model = UserSession``, as ``path:Class``."""
    found = []
    for name, source in sources.items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                    if isinstance(statement, ast.AnnAssign)
                    else []
                )
                value = getattr(statement, "value", None)
                if (
                    any(isinstance(t, ast.Name) and t.id == "model" for t in targets)
                    and isinstance(value, ast.Name)
                    and value.id == "UserSession"
                ):
                    found.append(f"{name}:{node.name}")
    return found


def _sources() -> dict[str, str]:
    return {
        path.relative_to(API_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SOURCE.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_one_repository_class_owns_user_sessions() -> None:
    assert session_repositories(_sources()) == [
        "src/modules/users/repository.py:UserSessionRepository"
    ]


def test_the_owner_guard_sees_a_second_copy() -> None:
    copy = "class UserSessionRepository(BaseRepository[UserSession]):\n    model = UserSession\n"
    sources = {**_sources(), "src/modules/auth/repository.py": copy}

    assert sorted(session_repositories(sources)) == [
        "src/modules/auth/repository.py:UserSessionRepository",
        "src/modules/users/repository.py:UserSessionRepository",
    ]


def test_auth_uses_the_owners_repositories_rather_than_its_own() -> None:
    assert auth_service.UserSessionRepository is users_repository.UserSessionRepository
    assert auth_service.UserRepository is users_repository.UserRepository
    assert not hasattr(auth_repository, "UserSessionRepository")
    assert not hasattr(auth_repository.PasswordResetTokenRepository, "invalidate_outstanding")
