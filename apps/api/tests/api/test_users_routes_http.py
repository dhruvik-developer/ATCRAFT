"""User and role administration over HTTP: success, refusal and tenant scope.

The rules themselves - the escalation guard, the portal boundary, the last
administrator - are pinned at service level in
``tests/integration/test_users_service.py``. These drive the seven write routes
no test had reached through HTTP: the status code and envelope each answers
with, the row it leaves behind and the audit entry written with it, the 422 its
payload or the record's state can earn, and the 404 another company's record
answers with - never a 403, which would confirm it exists.

The caller is the ``api_client`` principal - every permission, not a superuser
- unless a test narrows it. Prerequisites are built through the services, so a
fixture user or role has exactly the shape production gives one. Row state is
read with column-level SELECTs: role membership and session revocation are
written through the association table and ``UPDATE ... RETURNING``, which an
ORM instance already in the identity map does not see.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.permissions import Permission
from src.core.security import verify_password
from src.main import app
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.users.models import Role, User, UserSession, user_roles
from src.modules.users.schemas import RoleCreate, RoleDetail, UserCreate, UserDetail
from src.modules.users.service import RoleService, UserService
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

GOOD_PASSWORD = "Correct-Horse!2026"
NEW_PASSWORD = "Battery-Staple!2027"


# -----------------------------------------------------------------------------
# Callers, builders and readers
# -----------------------------------------------------------------------------


def address(prefix: str = "person") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@atcraft-qa.com"


def act_as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


def holding(base: Principal, *permissions: Permission) -> Principal:
    """The same person holding exactly these permissions."""
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=base.role_codes,
    )


@dataclass(frozen=True, slots=True)
class Foreign:
    """Another tenant, with an administrator able to build rows in it."""

    tenant: TenantContext
    principal: Principal


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> Foreign:
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return Foreign(
        tenant=TenantContext(company_id=other_company.id, user_id=owner.id),
        principal=Principal(
            user_id=owner.id,
            company_id=other_company.id,
            email=owner.email,
            full_name=owner.full_name,
            is_superuser=False,
            permissions=frozenset(str(p) for p in Permission),
        ),
    )


async def make_role(
    session: AsyncSession,
    tenant: TenantContext,
    principal: Principal,
    code: str,
    *permissions: Permission,
) -> RoleDetail:
    return await RoleService(session, tenant).create_role(
        principal,
        RoleCreate(code=code, name="Stores Role", permissions=[str(p) for p in permissions]),
    )


async def make_user(
    session: AsyncSession, tenant: TenantContext, principal: Principal, **overrides: Any
) -> UserDetail:
    payload: dict[str, Any] = {
        "email": address(),
        "full_name": "Store Keeper",
        "password": GOOD_PASSWORD,
        **overrides,
    }
    return await UserService(session, tenant).create_user(principal, UserCreate(**payload))


async def add_session(
    session: AsyncSession, company_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """A live refresh session for ``user_id``, built through the ORM."""
    record = UserSession(
        company_id=company_id,
        user_id=user_id,
        session_id=uuid.uuid4().hex,
        refresh_token_hash=uuid.uuid4().hex,
        refresh_jti=uuid.uuid4().hex,
        expires_at=utcnow() + timedelta(days=1),
    )
    session.add(record)
    await session.flush()
    return record.id


def data_of(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["message"], body
    return body["data"]


def error_of(response: Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is False, body
    assert body["error"]["message"], body
    return body["error"]


async def user_row(session: AsyncSession, user_id: uuid.UUID) -> Row[Any]:
    stmt = select(
        User.company_id,
        User.password_hash,
        User.must_change_password,
        User.is_active,
        User.failed_login_attempts,
        User.locked_until,
    ).where(User.id == user_id)
    return (await session.execute(stmt)).one()


async def held_roles(session: AsyncSession, user_id: uuid.UUID) -> list[tuple[uuid.UUID, Any]]:
    stmt = (
        select(user_roles.c.role_id, user_roles.c.assigned_by_id)
        .where(user_roles.c.user_id == user_id)
        .order_by(user_roles.c.role_id)
    )
    return [(row.role_id, row.assigned_by_id) for row in (await session.execute(stmt)).all()]


async def session_state(session: AsyncSession, row_id: uuid.UUID) -> Row[Any]:
    stmt = select(UserSession.revoked_at, UserSession.revoked_reason).where(
        UserSession.id == row_id
    )
    return (await session.execute(stmt)).one()


async def users_with_email(session: AsyncSession, company_id: uuid.UUID, email: str) -> int:
    stmt = (
        select(func.count())
        .select_from(User)
        .where(User.company_id == company_id, User.email == email)
    )
    return int((await session.execute(stmt)).scalar_one())


async def audit_rows(
    session: AsyncSession, company_id: uuid.UUID, entity_id: uuid.UUID, action: AuditAction
) -> list[Row[Any]]:
    stmt = (
        select(
            AuditLog.entity_type,
            AuditLog.created_by_id,
            AuditLog.old_values,
            AuditLog.new_values,
            AuditLog.context,
        )
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_id == entity_id,
            AuditLog.action == action,
        )
        .order_by(AuditLog.created_at, AuditLog.id)
    )
    return list((await session.execute(stmt)).all())


# -----------------------------------------------------------------------------
# POST /users
# -----------------------------------------------------------------------------


class TestCreateUser:
    async def test_a_user_is_created_with_its_roles_and_only_a_hash_of_the_password(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("STORES"), Permission.ITEM_VIEW
        )
        email = address("store.keeper")

        response = await api_client.post(
            "/users",
            json={
                "email": email.upper(),
                "full_name": "  Store   Keeper ",
                "password": GOOD_PASSWORD,
                "employee_code": unique_code("EMP"),
                "role_ids": [str(role.id)],
            },
        )

        data = data_of(response, 201)
        assert data["email"] == email
        assert data["full_name"] == "Store Keeper"
        assert data["role_codes"] == [role.code]
        assert [r["id"] for r in data["roles"]] == [str(role.id)]
        # An administrator chose this password, so it must be changed at sign-in.
        assert data["must_change_password"] is True
        assert data["is_superuser"] is False
        assert "password" not in data and "password_hash" not in data
        assert GOOD_PASSWORD not in response.text

        user_id = uuid.UUID(data["id"])
        stored = await user_row(session, user_id)
        assert stored.company_id == tenant.company_id
        assert verify_password(GOOD_PASSWORD, stored.password_hash)
        assert await held_roles(session, user_id) == [(role.id, tenant.user_id)]
        (created,) = await audit_rows(session, tenant.company_id, user_id, AuditAction.CREATE)
        assert created.entity_type == "user"
        assert created.created_by_id == tenant.user_id
        assert created.new_values["email"] == email
        assert "password_hash" not in created.new_values
        (granted,) = await audit_rows(session, tenant.company_id, user_id, AuditAction.ROLE_CHANGE)
        assert granted.new_values == {"roles": [role.code]}
        assert data_of(await api_client.get(f"/users/{user_id}"))["role_codes"] == [role.code]

    async def test_an_address_already_in_the_company_is_a_conflict(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        existing = await make_user(session, tenant, principal)

        response = await api_client.post(
            "/users",
            json={"email": existing.email.upper(), "full_name": "Twin", "password": GOOD_PASSWORD},
        )

        assert error_of(response, 409)["code"] == "DUPLICATE_DOCUMENT"
        assert await users_with_email(session, tenant.company_id, existing.email) == 1

    async def test_a_password_below_the_policy_names_the_field_and_writes_nothing(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        email = address()

        response = await api_client.post(
            "/users", json={"email": email, "full_name": "Weak Password", "password": "password"}
        )

        error = error_of(response, 422)
        assert error["code"] == "PASSWORD_POLICY"
        assert "password" in error["details"]["fields"]
        assert await users_with_email(session, tenant.company_id, email) == 0

    async def test_a_malformed_payload_names_each_field(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            "/users",
            json={
                "email": "nobody",
                "full_name": "   ",
                "password": GOOD_PASSWORD,
                "extra_permissions": ["item.vieww"],
            },
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"email", "full_name", "extra_permissions"} <= set(error["details"]["fields"])

    async def test_another_companys_role_cannot_be_granted(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        foreign: Foreign,
        unique_code,
    ) -> None:
        theirs = await make_role(
            session, foreign.tenant, foreign.principal, unique_code("THEIRS"), Permission.ITEM_VIEW
        )
        email = address()

        response = await api_client.post(
            "/users",
            json={
                "email": email,
                "full_name": "Borrowed Authority",
                "password": GOOD_PASSWORD,
                "role_ids": [str(theirs.id)],
            },
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await users_with_email(session, tenant.company_id, email) == 0


# -----------------------------------------------------------------------------
# POST /users/{user_id}/roles
# -----------------------------------------------------------------------------


class TestAssignRoles:
    async def test_the_role_set_is_replaced_and_both_sides_are_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        before = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )
        after = await make_role(
            session, tenant, principal, unique_code("PLANNER"), Permission.BOM_VIEW
        )
        target = await make_user(session, tenant, principal, role_ids=[before.id])

        response = await api_client.post(
            f"/users/{target.id}/roles",
            json={"role_ids": [str(after.id)], "reason": "Moved to planning"},
        )

        data = data_of(response)
        assert data["role_codes"] == [after.code]
        assert [r["code"] for r in data["roles"]] == [after.code]
        assert await held_roles(session, target.id) == [(after.id, tenant.user_id)]
        change = (await audit_rows(session, tenant.company_id, target.id, AuditAction.ROLE_CHANGE))[
            -1
        ]
        assert change.old_values == {"roles": [before.code]}
        assert change.new_values == {"roles": [after.code]}
        assert change.context == {"reason": "Moved to planning"}
        assert change.created_by_id == tenant.user_id

    async def test_resubmitting_the_current_set_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )
        target = await make_user(session, tenant, principal, role_ids=[role.id])

        response = await api_client.post(
            f"/users/{target.id}/roles", json={"role_ids": [str(role.id)], "reason": "No change"}
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        changes = await audit_rows(session, tenant.company_id, target.id, AuditAction.ROLE_CHANGE)
        assert len(changes) == 1  # only the initial grant at creation

    async def test_a_reason_is_required_and_unknown_fields_are_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)

        missing = await api_client.post(f"/users/{target.id}/roles", json={"role_ids": []})
        error = error_of(missing, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "reason" in error["details"]["fields"]

        smuggled = await api_client.post(
            f"/users/{target.id}/roles",
            json={"role_ids": [], "reason": "Cleanup", "is_superuser": True},
        )
        assert error_of(smuggled, 422)["code"] == "VALIDATION_ERROR"

    async def test_another_companys_user_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: Foreign, unique_code
    ) -> None:
        role = await make_role(
            session, foreign.tenant, foreign.principal, unique_code("THEIRS"), Permission.ITEM_VIEW
        )
        theirs = await make_user(session, foreign.tenant, foreign.principal, role_ids=[role.id])

        response = await api_client.post(
            f"/users/{theirs.id}/roles", json={"role_ids": [], "reason": "Strip their roles"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert [role_id for role_id, _ in await held_roles(session, theirs.id)] == [role.id]


# -----------------------------------------------------------------------------
# POST /users/{user_id}/deactivate and /activate
# -----------------------------------------------------------------------------


class TestDeactivateUser:
    async def test_deactivation_switches_the_account_off_and_ends_its_sessions(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)
        live = await add_session(session, tenant.company_id, target.id)

        response = await api_client.post(
            f"/users/{target.id}/deactivate", json={"reason": "Left the company"}
        )

        assert data_of(response)["is_active"] is False
        assert (await user_row(session, target.id)).is_active is False
        ended = await session_state(session, live)
        assert ended.revoked_at is not None
        assert ended.revoked_reason == "user_deactivated"
        (change,) = await audit_rows(
            session, tenant.company_id, target.id, AuditAction.STATUS_CHANGE
        )
        assert change.old_values == {"status": "ACTIVE"}
        assert change.new_values == {"status": "INACTIVE"}
        assert change.context["reason"].startswith("Left the company")
        assert change.created_by_id == tenant.user_id

    async def test_an_administrator_cannot_deactivate_their_own_account(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext
    ) -> None:
        response = await api_client.post(
            f"/users/{tenant.user_id}/deactivate", json={"reason": "Trying it on myself"}
        )

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert (await user_row(session, tenant.user_id)).is_active is True

    async def test_deactivating_an_inactive_account_is_an_invalid_transition(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal, is_active=False)

        response = await api_client.post(
            f"/users/{target.id}/deactivate", json={"reason": "Already off"}
        )

        error = error_of(response, 422)
        assert error["code"] == "INVALID_STATE_TRANSITION"
        assert error["details"]["current_status"] == "INACTIVE"

    async def test_a_lifecycle_change_needs_a_reason(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)

        response = await api_client.post(f"/users/{target.id}/deactivate", json={"reason": "x"})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "reason" in error["details"]["fields"]
        assert (await user_row(session, target.id)).is_active is True

    async def test_another_companys_user_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: Foreign
    ) -> None:
        theirs = await make_user(session, foreign.tenant, foreign.principal)

        response = await api_client.post(
            f"/users/{theirs.id}/deactivate", json={"reason": "Not ours to switch off"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert (await user_row(session, theirs.id)).is_active is True


class TestActivateUser:
    async def test_reactivation_lets_the_person_back_in_and_clears_the_lockout(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal, is_active=False)
        record = await session.get(User, target.id)
        assert record is not None
        record.failed_login_attempts = 4
        record.locked_until = utcnow() + timedelta(hours=1)
        await session.flush()

        response = await api_client.post(
            f"/users/{target.id}/activate", json={"reason": "Rejoined after leave"}
        )

        data = data_of(response)
        assert data["is_active"] is True
        assert data["failed_login_attempts"] == 0
        assert data["locked_until"] is None
        stored = await user_row(session, target.id)
        assert (stored.is_active, stored.failed_login_attempts, stored.locked_until) == (
            True,
            0,
            None,
        )
        (change,) = await audit_rows(
            session, tenant.company_id, target.id, AuditAction.STATUS_CHANGE
        )
        assert change.old_values == {"status": "INACTIVE"}
        assert change.new_values == {"status": "ACTIVE"}
        assert change.context == {"reason": "Rejoined after leave"}

    async def test_activating_an_active_account_is_an_invalid_transition(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)

        response = await api_client.post(
            f"/users/{target.id}/activate", json={"reason": "Already on"}
        )

        assert error_of(response, 422)["code"] == "INVALID_STATE_TRANSITION"
        assert not await audit_rows(
            session, tenant.company_id, target.id, AuditAction.STATUS_CHANGE
        )

    async def test_another_companys_user_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: Foreign
    ) -> None:
        theirs = await make_user(session, foreign.tenant, foreign.principal, is_active=False)

        response = await api_client.post(
            f"/users/{theirs.id}/activate", json={"reason": "Not ours to switch on"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert (await user_row(session, theirs.id)).is_active is False


# -----------------------------------------------------------------------------
# POST /users/{user_id}/reset-password
# -----------------------------------------------------------------------------


class TestResetPassword:
    async def test_a_generated_password_is_returned_once_works_and_is_never_audited(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal, must_change_password=False)
        live = await add_session(session, tenant.company_id, target.id)

        response = await api_client.post(
            f"/users/{target.id}/reset-password", json={"reason": "Forgot it at shift change"}
        )

        data = data_of(response)
        temporary = data["temporary_password"]
        assert temporary
        assert data["user_id"] == str(target.id)
        assert data["must_change_password"] is True
        assert data["sessions_revoked"] == 1
        stored = await user_row(session, target.id)
        assert verify_password(temporary, stored.password_hash)
        assert stored.must_change_password is True
        assert (await session_state(session, live)).revoked_reason == "password_reset"
        (audit,) = await audit_rows(
            session, tenant.company_id, target.id, AuditAction.PASSWORD_CHANGED
        )
        assert audit.context["password_generated"] is True
        assert audit.context["sessions_revoked"] == 1
        assert temporary not in json.dumps([audit.old_values, audit.new_values, audit.context])

    async def test_a_chosen_password_is_policed_and_never_echoed(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)

        weak = await api_client.post(
            f"/users/{target.id}/reset-password",
            json={"new_password": "password", "reason": "Locked out"},
        )
        error = error_of(weak, 422)
        assert error["code"] == "PASSWORD_POLICY"
        assert "new_password" in error["details"]["fields"]
        assert verify_password(GOOD_PASSWORD, (await user_row(session, target.id)).password_hash)

        chosen = await api_client.post(
            f"/users/{target.id}/reset-password",
            json={"new_password": NEW_PASSWORD, "reason": "Locked out"},
        )
        data = data_of(chosen)
        assert data["temporary_password"] is None
        assert NEW_PASSWORD not in chosen.text
        assert verify_password(NEW_PASSWORD, (await user_row(session, target.id)).password_hash)

    async def test_a_super_administrators_password_is_out_of_reach(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)
        record = await session.get(User, target.id)
        assert record is not None
        record.is_superuser = True
        await session.flush()

        response = await api_client.post(
            f"/users/{target.id}/reset-password", json={"reason": "Take over the platform"}
        )

        assert error_of(response, 403)["code"] == "PERMISSION_DENIED"
        assert verify_password(GOOD_PASSWORD, (await user_row(session, target.id)).password_hash)

    async def test_unknown_fields_are_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        target = await make_user(session, tenant, principal)

        response = await api_client.post(
            f"/users/{target.id}/reset-password",
            json={"reason": "Locked out", "must_change_password": False},
        )

        assert error_of(response, 422)["code"] == "VALIDATION_ERROR"
        assert verify_password(GOOD_PASSWORD, (await user_row(session, target.id)).password_hash)

    async def test_another_companys_user_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: Foreign
    ) -> None:
        theirs = await make_user(session, foreign.tenant, foreign.principal)

        response = await api_client.post(
            f"/users/{theirs.id}/reset-password", json={"reason": "Not ours to reset"}
        )

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert verify_password(GOOD_PASSWORD, (await user_row(session, theirs.id)).password_hash)


# -----------------------------------------------------------------------------
# POST /roles
# -----------------------------------------------------------------------------


class TestCreateRole:
    async def test_a_role_is_created_with_a_normalised_code_and_a_sorted_permission_set(
        self, api_client: AsyncClient, session: AsyncSession, tenant: TenantContext, unique_code
    ) -> None:
        raw = unique_code("purchase lead").lower()
        expected = raw.upper().replace("-", "_").replace(" ", "_")

        response = await api_client.post(
            "/roles",
            json={
                "code": raw,
                "name": "  Purchase   Lead ",
                "description": "  Raises purchase orders  ",
                "permissions": ["item.view", "bom.view", "item.view"],
            },
        )

        data = data_of(response, 201)
        assert data["code"] == expected
        assert data["name"] == "Purchase Lead"
        assert data["description"] == "Raises purchase orders"
        assert data["permissions"] == ["bom.view", "item.view"]
        assert (data["permission_count"], data["user_count"]) == (2, 0)
        assert data["is_system"] is False
        role_id = uuid.UUID(data["id"])
        stored = (
            await session.execute(
                select(Role.company_id, Role.code, Role.permissions).where(Role.id == role_id)
            )
        ).one()
        assert stored.company_id == tenant.company_id
        assert stored.code == expected
        assert sorted(stored.permissions) == ["bom.view", "item.view"]
        (created,) = await audit_rows(session, tenant.company_id, role_id, AuditAction.CREATE)
        assert created.entity_type == "role"
        assert created.created_by_id == tenant.user_id
        assert created.new_values["code"] == expected
        assert data_of(await api_client.get(f"/roles/{role_id}"))["code"] == expected

    async def test_a_code_already_in_use_is_a_conflict(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        existing = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.post(
            "/roles", json={"code": existing.code.lower(), "name": "Second Buyer"}
        )

        assert error_of(response, 409)["code"] == "DUPLICATE_DOCUMENT"

    async def test_a_malformed_role_names_each_field(self, api_client: AsyncClient) -> None:
        response = await api_client.post(
            "/roles", json={"code": "9-lives", "name": "", "permissions": ["item.vieww"]}
        )

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert {"code", "name", "permissions"} <= set(error["details"]["fields"])

    async def test_a_role_name_must_fit_its_column(
        self, api_client: AsyncClient, unique_code
    ) -> None:
        longest = "R" * 120
        created = data_of(
            await api_client.post("/roles", json={"code": unique_code("LONG"), "name": longest}),
            201,
        )
        assert created["name"] == longest

        too_long = {"name": "R" * 121}
        refused = await api_client.post("/roles", json={"code": unique_code("LONG"), **too_long})
        assert "name" in error_of(refused, 422)["details"]["fields"]
        renamed = await api_client.patch(f"/roles/{created['id']}", json=too_long)
        assert "name" in error_of(renamed, 422)["details"]["fields"]
        assert data_of(await api_client.get(f"/roles/{created['id']}"))["name"] == longest

    async def test_a_role_cannot_carry_a_permission_its_creator_lacks(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        act_as(
            holding(principal, Permission.ROLE_MANAGE, Permission.ROLE_VIEW, Permission.ITEM_VIEW)
        )
        code = unique_code("APPROVER").replace("-", "_")

        response = await api_client.post(
            "/roles",
            json={"code": code, "name": "Payment Approver", "permissions": ["payment.approve"]},
        )

        error = error_of(response, 403)
        assert error["code"] == "PERMISSION_DENIED"
        assert error["details"]["missing_permissions"] == ["payment.approve"]
        created = await session.scalar(
            select(func.count())
            .select_from(Role)
            .where(Role.company_id == tenant.company_id, Role.code == code)
        )
        assert created == 0


# -----------------------------------------------------------------------------
# PATCH /roles/{role_id}
# -----------------------------------------------------------------------------


class TestUpdateRole:
    async def test_name_and_permissions_change_and_the_holders_are_counted(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )
        await make_user(session, tenant, principal, role_ids=[role.id])

        response = await api_client.patch(
            f"/roles/{role.id}",
            json={"name": " Senior  Buyer ", "permissions": ["item.view", "bom.view"]},
        )

        data = data_of(response)
        assert data["name"] == "Senior Buyer"
        assert data["permissions"] == ["bom.view", "item.view"]
        assert data["user_count"] == 1
        stored = (
            await session.execute(select(Role.name, Role.permissions).where(Role.id == role.id))
        ).one()
        assert stored.name == "Senior Buyer"
        assert sorted(stored.permissions) == ["bom.view", "item.view"]
        (update,) = await audit_rows(session, tenant.company_id, role.id, AuditAction.UPDATE)
        assert update.old_values["name"] == "Stores Role"
        assert update.new_values["name"] == "Senior Buyer"
        (widened,) = await audit_rows(
            session, tenant.company_id, role.id, AuditAction.PERMISSION_CHANGE
        )
        assert widened.new_values == {"permissions": ["bom.view", "item.view"]}
        assert widened.context == {"affected_user_count": 1}

    async def test_suspending_a_role_is_recorded_as_a_lifecycle_change(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{role.id}", json={"is_active": False})

        assert data_of(response)["is_active"] is False
        assert await session.scalar(select(Role.is_active).where(Role.id == role.id)) is False
        (suspended,) = await audit_rows(
            session, tenant.company_id, role.id, AuditAction.ROLE_CHANGE
        )
        assert suspended.old_values == {"status": "ACTIVE"}
        assert suspended.new_values == {"status": "INACTIVE"}

    async def test_an_empty_patch_is_refused(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{role.id}", json={})

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        assert not await audit_rows(session, tenant.company_id, role.id, AuditAction.UPDATE)

    async def test_the_code_cannot_be_patched(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{role.id}", json={"code": "RENAMED_ROLE"})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "code" in error["details"]["fields"]
        assert await session.scalar(select(Role.code).where(Role.id == role.id)) == role.code

    async def test_the_super_admin_role_is_immutable(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        super_admin = await make_role(
            session, tenant, principal, "SUPER_ADMIN", Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{super_admin.id}", json={"permissions": []})

        assert error_of(response, 422)["code"] == "BUSINESS_RULE_VIOLATION"
        stored = await session.scalar(select(Role.permissions).where(Role.id == super_admin.id))
        assert stored == ["item.view"]

    async def test_a_null_name_is_a_field_error(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
    ) -> None:
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{role.id}", json={"name": None})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert "name" in error["details"]["fields"]
        assert await session.scalar(select(Role.name).where(Role.id == role.id)) == "Stores Role"

    @pytest.mark.parametrize("field", ["is_active", "permissions"])
    async def test_a_null_for_another_required_column_names_the_field(
        self,
        api_client: AsyncClient,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        unique_code,
        field: str,
    ) -> None:
        """The same contract ``name`` already keeps, for the role's other columns."""
        role = await make_role(
            session, tenant, principal, unique_code("BUYER"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{role.id}", json={field: None})

        error = error_of(response, 422)
        assert error["code"] == "VALIDATION_ERROR"
        assert field in (error["details"] or {}).get("fields", {})

    async def test_another_companys_role_is_not_found(
        self, api_client: AsyncClient, session: AsyncSession, foreign: Foreign, unique_code
    ) -> None:
        theirs = await make_role(
            session, foreign.tenant, foreign.principal, unique_code("THEIRS"), Permission.ITEM_VIEW
        )

        response = await api_client.patch(f"/roles/{theirs.id}", json={"name": "Taken over"})

        assert error_of(response, 404)["code"] == "NOT_FOUND"
        assert await session.scalar(select(Role.name).where(Role.id == theirs.id)) == "Stores Role"
