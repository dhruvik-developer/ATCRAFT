"""Recovery identity changes exercise credential authority, not profile editing."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, get_current_principal
from src.core.permissions import Permission
from src.main import app
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.users.models import PasswordResetToken, Role, User, UserSession
from src.modules.users.schemas import UserCreate
from src.modules.users.service import UserService

pytestmark = [pytest.mark.api, pytest.mark.integration]


def caller(base: Principal, *, reset: bool = False, superuser: bool = False) -> Principal:
    permissions = {str(Permission.USER_UPDATE), str(Permission.USER_VIEW)}
    if reset:
        permissions.add(str(Permission.USER_RESET_PASSWORD))
    return Principal(
        user_id=base.user_id,
        company_id=base.company_id,
        email=base.email,
        full_name=base.full_name,
        is_superuser=superuser,
        permissions=frozenset(permissions),
    )


async def target(session, tenant, principal, protected: str = "ordinary"):
    roles = []
    if protected == "role":
        role = Role(
            company_id=tenant.company_id,
            code="SUPER_ADMIN",
            name="Super Administrator",
            permissions=[str(p) for p in Permission],
            is_active=True,
            is_system=True,
        )
        session.add(role)
        await session.flush()
        roles = [role.id]
    created = await UserService(session, tenant).create_user(
        principal,
        UserCreate(
            email=f"recovery-{uuid.uuid4().hex}@example.com",
            full_name="Recovery Owner",
            password="Correct-Horse!2026",
            role_ids=roles,
        ),
    )
    user = await session.get(User, created.id)
    if protected == "flag":
        user.is_superuser = True
        await session.flush()
    return user


@pytest.mark.parametrize("protected", ["ordinary", "flag", "role"])
async def test_profile_editor_cannot_change_recovery_email(
    api_client, session, tenant, principal, protected
):
    user = await target(session, tenant, principal, protected)
    user_id, old_email = user.id, user.email
    app.dependency_overrides[get_current_principal] = lambda: caller(principal)
    response = await api_client.patch(
        f"/users/{user_id}",
        json={"email": "replacement@example.com", "full_name": "Changed"},
    )
    assert response.status_code == 403
    await session.refresh(user)
    assert user.email == old_email
    assert user.full_name == "Recovery Owner"


@pytest.mark.parametrize("protected", ["flag", "role"])
async def test_reset_holder_cannot_change_protected_recovery_email(
    api_client, session, tenant, principal, protected
):
    user = await target(session, tenant, principal, protected)
    user_id, old_email = user.id, user.email
    app.dependency_overrides[get_current_principal] = lambda: caller(principal, reset=True)
    response = await api_client.patch(
        f"/users/{user_id}", json={"email": "replacement@example.com"}
    )
    assert response.status_code == 403
    await session.refresh(user)
    assert user.email == old_email


async def test_same_normalized_email_remains_a_profile_edit(api_client, session, tenant, principal):
    user = await target(session, tenant, principal)
    app.dependency_overrides[get_current_principal] = lambda: caller(principal)
    response = await api_client.patch(
        f"/users/{user.id}",
        json={"email": user.email.upper(), "full_name": "Allowed name"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Allowed name"


@pytest.mark.parametrize("protected", ["ordinary", "flag", "role"])
async def test_authorized_change_preserves_mfa_and_burns_old_recovery(
    api_client, session: AsyncSession, tenant, principal, protected, monkeypatch
):
    user = await target(session, tenant, principal, protected)
    user_id = user.id
    user.mfa_enabled = True
    user.mfa_secret = "synthetic-encrypted-secret"
    expires = utcnow() + timedelta(days=1)
    grant = PasswordResetToken(
        company_id=tenant.company_id,
        user_id=user_id,
        token_hash=uuid.uuid4().hex,
        email_token_ciphertext="synthetic-delivery-copy",
        expires_at=expires,
    )
    login = UserSession(
        company_id=tenant.company_id,
        user_id=user_id,
        session_id=uuid.uuid4().hex,
        refresh_token_hash="synthetic-digest",
        refresh_jti=uuid.uuid4().hex,
        expires_at=expires,
    )
    session.add_all([grant, login])
    await session.flush()
    cached = []

    async def remember(key, value, *, ttl):
        cached.append((key, value, ttl))

    monkeypatch.setattr("src.modules.users.service.cache_set", remember)
    app.dependency_overrides[get_current_principal] = lambda: caller(
        principal, reset=True, superuser=protected != "ordinary"
    )
    response = await api_client.patch(
        f"/users/{user_id}",
        json={"email": f"changed-{uuid.uuid4().hex}@example.com"},
    )
    assert response.status_code == 200
    await session.refresh(user)
    await session.refresh(grant)
    await session.refresh(login)
    assert user.mfa_enabled is True
    assert user.mfa_secret == "synthetic-encrypted-secret"
    assert grant.used_at is not None and grant.email_token_ciphertext is None
    assert login.revoked_at is not None
    assert cached and cached[0][1] is False
    audits = (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == user_id,
                    AuditLog.action == AuditAction.UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert audits[-1].context["recovery_email_changed"] is True
    assert audits[-1].context["sessions_revoked"] == 1
    assert audits[-1].context["reset_grants_invalidated"] == 1
