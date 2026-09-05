"""Service-level tests for the users module, against a real PostgreSQL.

These cover what only a database can prove: tenant isolation on role
assignment, the ``user_roles`` rows written with their grantor, the audit
entries written in the same transaction as the change, session revocation on
deactivation, the last-super-administrator guard evaluated in SQL, and the fact
that a user list row genuinely does not load the ``roles`` relationship.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``,
``principal``) come from ``tests/conftest.py``. The session's outer transaction
is rolled back after every test, so nothing here survives into the next one.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import utcnow
from src.core.deps import Principal, TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.security import verify_password
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.users.models import Role, User, UserSession, user_roles
from src.modules.users.schemas import (
    PasswordResetRequest,
    RoleAssignment,
    RoleCreate,
    RoleFilters,
    RoleUpdate,
    UserCreate,
    UserFilters,
    UserUpdate,
)
from src.modules.users.service import (
    SUPER_ADMIN_PASSWORD_PROTECTED,
    SUPER_ADMIN_ROLE_CODES,
    RoleService,
    UserService,
)

pytestmark = pytest.mark.integration

GOOD_PASSWORD = "Correct-Horse!2026"


# -----------------------------------------------------------------------------
# Helpers and fixtures
# -----------------------------------------------------------------------------


def _payload(email: str, **overrides: Any) -> UserCreate:
    data: dict[str, Any] = {
        "email": email,
        "full_name": "Test Person",
        "password": GOOD_PASSWORD,
    }
    data.update(overrides)
    return UserCreate(**data)


def _email(prefix: str = "person") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _principal_without(principal: Principal, *permissions: Permission) -> Principal:
    """The same person, minus specific permissions."""
    dropped = {str(p) for p in permissions}
    return Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset(p for p in principal.permissions if p not in dropped),
        role_codes=principal.role_codes,
    )


def _principal_with(principal: Principal, *permissions: Permission) -> Principal:
    """The same person, holding only the listed permissions."""
    return Principal(
        user_id=principal.user_id,
        company_id=principal.company_id,
        email=principal.email,
        full_name=principal.full_name,
        is_superuser=False,
        permissions=frozenset(str(p) for p in permissions),
        role_codes=principal.role_codes,
    )


@pytest.fixture
async def buyer_role(session: AsyncSession, company) -> Role:
    role = Role(
        company_id=company.id,
        code=f"BUYER_{uuid.uuid4().hex[:6].upper()}",
        name="Buyer",
        permissions=[str(Permission.PURCHASE_ORDER_VIEW), str(Permission.PURCHASE_ORDER_CREATE)],
        is_system=False,
        is_vendor_role=False,
        is_active=True,
    )
    session.add(role)
    await session.flush()
    return role


@pytest.fixture
async def vendor_role(session: AsyncSession, company) -> Role:
    role = Role(
        company_id=company.id,
        code=f"VENDOR_{uuid.uuid4().hex[:6].upper()}",
        name="Vendor Portal User",
        permissions=[str(Permission.PURCHASE_ORDER_VIEW)],
        is_system=True,
        is_vendor_role=True,
        is_active=True,
    )
    session.add(role)
    await session.flush()
    return role


@pytest.fixture
async def foreign_role(session: AsyncSession, other_company) -> Role:
    """A role belonging to a different tenant. Must never be assignable."""
    role = Role(
        company_id=other_company.id,
        code=f"FOREIGN_{uuid.uuid4().hex[:6].upper()}",
        name="Someone Else's Role",
        permissions=[str(Permission.ITEM_VIEW)],
        is_system=False,
        is_active=True,
    )
    session.add(role)
    await session.flush()
    return role


@pytest.fixture
async def vendor(session: AsyncSession, company):
    from src.modules.vendors.models import Vendor, VendorType

    record = Vendor(
        company_id=company.id,
        code=f"V-{uuid.uuid4().hex[:6].upper()}",
        name="Acme Components",
        vendor_types=[str(VendorType.COMPONENT_SUPPLIER)],
        is_active=True,
    )
    session.add(record)
    await session.flush()
    return record


async def _audit_rows(
    session: AsyncSession, entity_id: uuid.UUID, action: AuditAction | None = None
) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list((await session.execute(stmt)).scalars().all())


# -----------------------------------------------------------------------------
# Creation
# -----------------------------------------------------------------------------


class TestCreateUser:
    async def test_password_is_hashed_and_never_returned(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        created = await UserService(session, tenant).create_user(principal, _payload(_email()))

        stored = (await session.execute(select(User).where(User.id == created.id))).scalar_one()
        assert stored.password_hash.startswith("$argon2")
        assert verify_password(GOOD_PASSWORD, stored.password_hash)
        assert stored.password_changed_at is not None
        # The response model has no field that could carry it.
        assert not hasattr(created, "password_hash")

    async def test_weak_password_is_refused_before_anything_is_written(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        address = _email()
        with pytest.raises(ValidationError) as excinfo:
            await UserService(session, tenant).create_user(
                principal, _payload(address, password="password")
            )
        assert excinfo.value.code is ErrorCode.PASSWORD_POLICY

        count_stmt = select(func.count()).select_from(User).where(User.email == address)
        remaining = (await session.execute(count_stmt)).scalar_one()
        assert remaining == 0

    async def test_duplicate_email_is_refused_within_the_company(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        address = _email()
        service = UserService(session, tenant)
        await service.create_user(principal, _payload(address))
        with pytest.raises(DuplicateError):
            await service.create_user(principal, _payload(address.upper()))

    async def test_creation_writes_an_audit_entry_in_the_same_transaction(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        created = await UserService(session, tenant).create_user(principal, _payload(_email()))

        rows = await _audit_rows(session, created.id, AuditAction.CREATE)
        assert len(rows) == 1
        assert rows[0].entity_type == "user"
        assert rows[0].created_by_id == tenant.user_id
        # The hash must not have reached the log even as a "new value".
        assert "password_hash" not in (rows[0].new_values or {})

    async def test_roles_are_recorded_with_who_granted_them(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, buyer_role: Role
    ) -> None:
        created = await UserService(session, tenant).create_user(
            principal, _payload(_email(), role_ids=[buyer_role.id])
        )

        row = (
            await session.execute(select(user_roles).where(user_roles.c.user_id == created.id))
        ).one()
        assert row.role_id == buyer_role.id
        # The whole reason membership is written against the table directly.
        assert row.assigned_by_id == tenant.user_id
        assert created.role_codes == [buyer_role.code]

        assert await _audit_rows(session, created.id, AuditAction.ROLE_CHANGE)

    async def test_unknown_organisational_reference_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        with pytest.raises(NotFoundError):
            await UserService(session, tenant).create_user(
                principal, _payload(_email(), branch_id=uuid.uuid4())
            )

    async def test_category_scope_without_a_vendor_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        # A silently discarded restriction reads, on the screen that set it, as
        # a restriction that is in force.
        with pytest.raises(BusinessRuleError):
            await UserService(session, tenant).create_user(
                principal, _payload(_email(), allowed_category_ids=[uuid.uuid4()])
            )

    async def test_direct_grants_require_the_assign_permission(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        without = _principal_without(principal, Permission.PERMISSION_ASSIGN)
        with pytest.raises(PermissionDeniedError):
            await UserService(session, tenant).create_user(
                without,
                _payload(_email(), extra_permissions=[str(Permission.PAYMENT_APPROVE)]),
            )

    async def test_direct_grants_cannot_exceed_the_callers_own_authority(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        weak = _principal_with(principal, Permission.USER_CREATE, Permission.PERMISSION_ASSIGN)
        with pytest.raises(PermissionDeniedError) as excinfo:
            await UserService(session, tenant).create_user(
                weak, _payload(_email(), extra_permissions=[str(Permission.PAYMENT_APPROVE)])
            )
        assert str(Permission.PAYMENT_APPROVE) in excinfo.value.details["missing_permissions"]


# -----------------------------------------------------------------------------
# Tenant isolation
# -----------------------------------------------------------------------------


class TestTenantIsolation:
    async def test_another_tenants_role_cannot_be_assigned(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        foreign_role: Role,
    ) -> None:
        # The repository query is tenant-scoped, so the role comes back missing
        # and is reported as not found rather than silently dropped.
        with pytest.raises(NotFoundError):
            await UserService(session, tenant).create_user(
                principal, _payload(_email(), role_ids=[foreign_role.id])
            )

    async def test_another_tenants_user_is_not_visible(
        self, session: AsyncSession, tenant: TenantContext, other_company
    ) -> None:
        from src.core.security import hash_password

        stranger = User(
            company_id=other_company.id,
            email=_email("stranger"),
            full_name="Stranger",
            password_hash=hash_password(GOOD_PASSWORD),
            is_active=True,
        )
        session.add(stranger)
        await session.flush()

        with pytest.raises(NotFoundError):
            await UserService(session, tenant).get_user(stranger.id)

    async def test_another_tenants_branch_cannot_be_referenced(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, other_company
    ) -> None:
        # The foreign key alone would accept it: branches.id is globally unique.
        from src.modules.organization.models import Branch

        branch = Branch(
            company_id=other_company.id,
            code=f"B{uuid.uuid4().hex[:4].upper()}",
            name="Their Head Office",
            is_active=True,
        )
        session.add(branch)
        await session.flush()

        with pytest.raises(NotFoundError):
            await UserService(session, tenant).create_user(
                principal, _payload(_email(), branch_id=branch.id)
            )


# -----------------------------------------------------------------------------
# Vendor-portal isolation, end to end
# -----------------------------------------------------------------------------


class TestVendorIsolation:
    async def test_vendor_role_on_an_internal_account_is_refused(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
    ) -> None:
        with pytest.raises(BusinessRuleError) as excinfo:
            await UserService(session, tenant).create_user(
                principal, _payload(_email(), role_ids=[vendor_role.id])
            )
        assert excinfo.value.details["vendor_roles"] == [vendor_role.code]

    async def test_internal_role_on_a_vendor_account_is_refused(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
        vendor,
    ) -> None:
        with pytest.raises(BusinessRuleError) as excinfo:
            await UserService(session, tenant).create_user(
                principal,
                _payload(_email(), vendor_id=vendor.id, role_ids=[buyer_role.id]),
            )
        assert excinfo.value.details["internal_roles"] == [buyer_role.code]

    async def test_a_matching_vendor_account_is_created(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
        vendor,
    ) -> None:
        created = await UserService(session, tenant).create_user(
            principal,
            _payload(
                _email(),
                vendor_id=vendor.id,
                role_ids=[vendor_role.id],
                allowed_category_ids=[uuid.uuid4()],
            ),
        )
        assert created.is_vendor_user
        assert created.role_codes == [vendor_role.code]
        assert len(created.allowed_category_ids) == 1

    async def test_detaching_the_vendor_while_vendor_roles_remain_is_refused(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
        vendor,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(
            principal, _payload(_email(), vendor_id=vendor.id, role_ids=[vendor_role.id])
        )
        # The boundary has to hold when the *user* moves, not only when the roles do.
        with pytest.raises(BusinessRuleError):
            await service.update_user(principal, created.id, UserUpdate(clear_vendor=True))


class TestVendorPortalIsolation:
    """A supplier's staff must not be able to read the internal directory.

    A vendor-portal account belongs to the same *company* row as the buyer's own
    employees - that is how the portal works - so the tenant filter alone does
    not separate them. ``VendorRepository`` has always carried a second clause
    for this and its docstring explains why it lives in the repository:
    "Applied here rather than in each endpoint so no caller can forget it".
    ``UserRepository`` had no such clause, and every endpoint reading users had
    forgotten it: a security review signed in as a vendor holding the seeded
    VENDOR_OWNER role (which includes ``user.view``) and listed all 14 internal
    staff, then read the platform administrator's row - name, email, roles,
    ``is_superuser`` - and its full 150-permission set.
    """

    async def test_a_vendor_principal_sees_only_its_own_portal_staff(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
        vendor,
    ) -> None:
        service = UserService(session, tenant)
        internal = await service.create_user(principal, _payload(_email(), full_name="Employee"))
        portal = await service.create_user(
            principal,
            _payload(_email(), vendor_id=vendor.id, role_ids=[vendor_role.id]),
        )

        # The same company, scoped as the vendor's own staff would be.
        vendor_tenant = TenantContext(
            company_id=tenant.company_id,
            user_id=portal.id,
            vendor_id=vendor.id,
        )
        rows, _ = await UserService(session, vendor_tenant).list_users(
            UserFilters(), PageParams(page=1, limit=100), SortParams()
        )

        visible = {row.id for row in rows}
        assert portal.id in visible, "a vendor user cannot see its own account"
        assert internal.id not in visible, (
            "a vendor-portal account listed an internal employee - the directory is exposed"
        )

    async def test_a_vendor_principal_cannot_read_an_internal_user_by_id(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
        vendor,
    ) -> None:
        """Knowing the id must not be enough - the list filter is not the guard."""
        service = UserService(session, tenant)
        internal = await service.create_user(principal, _payload(_email(), full_name="Employee"))
        portal = await service.create_user(
            principal,
            _payload(_email(), vendor_id=vendor.id, role_ids=[vendor_role.id]),
        )

        vendor_tenant = TenantContext(
            company_id=tenant.company_id,
            user_id=portal.id,
            vendor_id=vendor.id,
        )
        with pytest.raises(NotFoundError):
            await UserService(session, vendor_tenant).get_user(internal.id)

    async def test_an_internal_principal_still_sees_everyone(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        vendor_role: Role,
        vendor,
    ) -> None:
        """The restriction must not leak upward and blind the administrator."""
        service = UserService(session, tenant)
        internal = await service.create_user(principal, _payload(_email()))
        portal = await service.create_user(
            principal,
            _payload(_email(), vendor_id=vendor.id, role_ids=[vendor_role.id]),
        )

        rows, _ = await service.list_users(
            UserFilters(), PageParams(page=1, limit=100), SortParams()
        )
        visible = {row.id for row in rows}
        assert {internal.id, portal.id} <= visible


class TestPasswordAuthority:
    """Setting somebody's password is not part of ``user.update``.

    The two rights are deliberately separate - there is a dedicated
    ``POST /users/{id}/reset-password`` gated on ``user.reset_password``, and a
    helpdesk role is meant to hold that one and not the other. Without a guard
    inside ``update_user`` the split was decorative: a security review
    reproduced full account takeover through ``PATCH /users/{id}`` with a
    ``password`` field, against a SUPER_ADMIN target, using only ``user.update``.
    """

    async def test_a_password_cannot_be_set_with_user_update_alone(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        service = UserService(session, tenant)
        victim = await service.create_user(principal, _payload(_email()))

        clerk = _principal_with(principal, Permission.USER_VIEW, Permission.USER_UPDATE)
        with pytest.raises(PermissionDeniedError):
            await service.update_user(clerk, victim.id, UserUpdate(password="Hijack3d!Pass99xZ"))

    async def test_the_same_caller_may_still_edit_everything_else(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        """The guard must not cost ``user.update`` its actual job."""
        service = UserService(session, tenant)
        victim = await service.create_user(principal, _payload(_email()))

        clerk = _principal_with(principal, Permission.USER_VIEW, Permission.USER_UPDATE)
        updated = await service.update_user(clerk, victim.id, UserUpdate(full_name="Renamed"))
        assert updated.full_name == "Renamed"

    async def test_a_holder_of_the_reset_right_may_set_one(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        service = UserService(session, tenant)
        victim = await service.create_user(principal, _payload(_email()))

        helpdesk = _principal_with(
            principal,
            Permission.USER_VIEW,
            Permission.USER_UPDATE,
            Permission.USER_RESET_PASSWORD,
        )
        await service.update_user(helpdesk, victim.id, UserUpdate(password="Chosen!Pass99xZ"))

        stored = await session.get(User, victim.id)
        assert stored is not None
        assert verify_password("Chosen!Pass99xZ", stored.password_hash)

    async def test_the_refusal_happens_before_anything_is_written(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
    ) -> None:
        """A rejected call must not leave the other fields changed.

        An authority check part-way through an update is one a later edit can
        step over; this pins that the whole payload is refused together.
        """
        service = UserService(session, tenant)
        victim = await service.create_user(principal, _payload(_email(), full_name="Original"))

        clerk = _principal_with(principal, Permission.USER_VIEW, Permission.USER_UPDATE)
        with pytest.raises(PermissionDeniedError):
            await service.update_user(
                clerk,
                victim.id,
                UserUpdate(full_name="Changed too", password="Hijack3d!Pass99xZ"),
            )

        stored = await session.get(User, victim.id)
        assert stored is not None
        assert stored.full_name == "Original"


# -----------------------------------------------------------------------------
# Role assignment
# -----------------------------------------------------------------------------


class TestRoleAssignment:
    async def test_replacing_the_set_adds_and_removes(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
        company,
    ) -> None:
        other = Role(
            company_id=company.id,
            code=f"STORE_{uuid.uuid4().hex[:6].upper()}",
            name="Store",
            permissions=[str(Permission.INVENTORY_VIEW)],
            is_active=True,
        )
        session.add(other)
        await session.flush()

        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email(), role_ids=[buyer_role.id]))

        updated = await service.assign_roles(
            principal, created.id, RoleAssignment(role_ids=[other.id], reason="Moved to stores")
        )
        assert updated.role_codes == [other.code]

        rows = (
            (
                await session.execute(
                    select(user_roles.c.role_id).where(user_roles.c.user_id == created.id)
                )
            )
            .scalars()
            .all()
        )
        assert set(rows) == {other.id}

    async def test_an_unchanged_set_is_refused_rather_than_audited_as_a_change(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email(), role_ids=[buyer_role.id]))
        with pytest.raises(BusinessRuleError):
            await service.assign_roles(
                principal,
                created.id,
                RoleAssignment(role_ids=[buyer_role.id], reason="No change"),
            )

    async def test_assignment_writes_a_role_change_audit_with_both_sets(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))
        await service.assign_roles(
            principal,
            created.id,
            RoleAssignment(role_ids=[buyer_role.id], reason="Joined procurement"),
        )

        rows = await _audit_rows(session, created.id, AuditAction.ROLE_CHANGE)
        assert rows
        latest = rows[-1]
        assert latest.new_values == {"roles": [buyer_role.code]}
        assert latest.context is not None and latest.context["reason"] == "Joined procurement"

    async def test_a_suspended_role_cannot_be_assigned(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        buyer_role.is_active = False
        await session.flush()

        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))
        with pytest.raises(BusinessRuleError):
            await service.assign_roles(
                principal,
                created.id,
                RoleAssignment(role_ids=[buyer_role.id], reason="Should fail"),
            )

    async def test_caller_cannot_assign_a_role_beyond_their_own_authority(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        company,
    ) -> None:
        powerful = Role(
            company_id=company.id,
            code=f"TREASURY_{uuid.uuid4().hex[:5].upper()}",
            name="Treasury",
            permissions=[str(Permission.PAYMENT_APPROVE)],
            is_active=True,
        )
        session.add(powerful)
        await session.flush()

        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))

        weak = _principal_with(principal, Permission.PERMISSION_ASSIGN, Permission.USER_VIEW)
        with pytest.raises(PermissionDeniedError):
            await service.assign_roles(
                weak,
                created.id,
                RoleAssignment(role_ids=[powerful.id], reason="Escalation attempt"),
            )


# -----------------------------------------------------------------------------
# Lifecycle, sessions and the last-administrator guard
# -----------------------------------------------------------------------------


class TestLifecycle:
    async def test_deactivation_revokes_every_live_session(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))

        for index in range(2):
            session.add(
                UserSession(
                    company_id=company.id,
                    user_id=created.id,
                    session_id=uuid.uuid4().hex,
                    refresh_token_hash=f"hash{index}",
                    refresh_jti=uuid.uuid4().hex,
                    expires_at=utcnow().replace(year=utcnow().year + 1),
                )
            )
        await session.flush()

        await service.set_active(principal, created.id, active=False, reason="Left the company")

        live = (
            await session.execute(
                select(func.count())
                .select_from(UserSession)
                .where(UserSession.user_id == created.id, UserSession.revoked_at.is_(None))
            )
        ).scalar_one()
        assert live == 0

    async def test_reactivation_clears_the_lockout(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email(), is_active=False))

        stored = (await session.execute(select(User).where(User.id == created.id))).scalar_one()
        stored.failed_login_attempts = 5
        stored.locked_until = utcnow().replace(year=utcnow().year + 1)
        await session.flush()

        reactivated = await service.set_active(
            principal, created.id, active=True, reason="Returned from leave"
        )
        assert reactivated.is_active
        assert reactivated.failed_login_attempts == 0
        assert reactivated.locked_until is None

    async def test_you_cannot_retire_your_own_account(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        with pytest.raises(BusinessRuleError):
            await UserService(session, tenant).delete_user(tenant.user_id, reason="Oops")

    async def test_retirement_is_soft_and_revokes_sessions(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))
        session.add(
            UserSession(
                company_id=company.id,
                user_id=created.id,
                session_id=uuid.uuid4().hex,
                refresh_token_hash="h",
                refresh_jti=uuid.uuid4().hex,
                expires_at=utcnow().replace(year=utcnow().year + 1),
            )
        )
        await session.flush()

        await service.delete_user(created.id, reason="Left the company")

        stored = (await session.execute(select(User).where(User.id == created.id))).scalar_one()
        # The row survives: users.id is referenced by every document the person
        # created and by every audit row describing what they did.
        assert stored.deleted_at is not None
        assert (
            await session.execute(
                select(UserSession.revoked_at).where(UserSession.user_id == created.id)
            )
        ).scalar_one() is not None

        with pytest.raises(NotFoundError):
            await service.get_user(created.id)

    async def test_the_last_super_administrator_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        super_role = Role(
            company_id=company.id,
            code=next(iter(SUPER_ADMIN_ROLE_CODES)),
            name="Super Administrator",
            permissions=[str(p) for p in Permission],
            is_system=True,
            is_active=True,
        )
        session.add(super_role)
        await session.flush()

        service = UserService(session, tenant)
        only_admin = await service.create_user(
            principal, _payload(_email("root"), role_ids=[super_role.id])
        )

        with pytest.raises(BusinessRuleError) as excinfo:
            await service.delete_user(only_admin.id, reason="Cleanup")
        assert excinfo.value.details["user_id"] == str(only_admin.id)

        # Deactivation is guarded by the same rule, for the same reason.
        with pytest.raises(BusinessRuleError):
            await service.set_active(principal, only_admin.id, active=False, reason="Cleanup")

        # And so is removing the authority by reassigning roles.
        with pytest.raises(BusinessRuleError):
            await service.assign_roles(
                principal, only_admin.id, RoleAssignment(role_ids=[], reason="Cleanup")
            )

    async def test_a_second_administrator_makes_the_first_removable(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        super_role = Role(
            company_id=company.id,
            code=next(iter(SUPER_ADMIN_ROLE_CODES)),
            name="Super Administrator",
            permissions=[str(p) for p in Permission],
            is_system=True,
            is_active=True,
        )
        session.add(super_role)
        await session.flush()

        service = UserService(session, tenant)
        first = await service.create_user(
            principal, _payload(_email("root1"), role_ids=[super_role.id])
        )
        await service.create_user(principal, _payload(_email("root2"), role_ids=[super_role.id]))

        await service.delete_user(first.id, reason="Handed over")
        stored = (await session.execute(select(User).where(User.id == first.id))).scalar_one()
        assert stored.deleted_at is not None


# -----------------------------------------------------------------------------
# Password reset
# -----------------------------------------------------------------------------


class TestPasswordReset:
    async def test_generated_password_works_and_forces_a_change(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(
            principal, _payload(_email(), must_change_password=False)
        )

        result = await service.reset_password(
            principal, created.id, PasswordResetRequest(reason="Forgot it")
        )
        assert result.temporary_password is not None
        assert result.must_change_password

        stored = (await session.execute(select(User).where(User.id == created.id))).scalar_one()
        assert verify_password(result.temporary_password, stored.password_hash)
        assert stored.must_change_password
        assert not verify_password(GOOD_PASSWORD, stored.password_hash)

    async def test_reset_revokes_sessions_and_is_audited_without_the_password(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))
        session.add(
            UserSession(
                company_id=company.id,
                user_id=created.id,
                session_id=uuid.uuid4().hex,
                refresh_token_hash="h",
                refresh_jti=uuid.uuid4().hex,
                expires_at=utcnow().replace(year=utcnow().year + 1),
            )
        )
        await session.flush()

        result = await service.reset_password(
            principal, created.id, PasswordResetRequest(reason="Credentials exposed")
        )
        assert result.sessions_revoked == 1

        rows = await _audit_rows(session, created.id, AuditAction.PASSWORD_CHANGED)
        assert len(rows) == 1
        serialised = str(rows[0].context) + str(rows[0].new_values) + str(rows[0].old_values)
        assert result.temporary_password not in serialised

    async def test_an_administrator_supplied_password_is_policed(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email()))
        with pytest.raises(ValidationError):
            await service.reset_password(
                principal, created.id, PasswordResetRequest(new_password="short", reason="Nope")
            )

    # -- super-administrator targets ------------------------------------------
    #
    # ``user.reset_password`` is an ordinary administrative right. On its own it
    # must not be enough to take over a super-administrator's account by either
    # route a password can be set (PROTECT_SUPER_ADMIN_PASSWORDS).

    @staticmethod
    async def _super_role(session: AsyncSession, company) -> Role:
        role = Role(
            company_id=company.id,
            code=next(iter(SUPER_ADMIN_ROLE_CODES)),
            name="Super Administrator",
            permissions=[str(p) for p in Permission],
            is_system=True,
            is_active=True,
        )
        session.add(role)
        await session.flush()
        return role

    @staticmethod
    def _assert_protected(excinfo: pytest.ExceptionInfo[AuthorizationError]) -> None:
        assert excinfo.value.code == ErrorCode.PERMISSION_DENIED
        assert excinfo.value.message == SUPER_ADMIN_PASSWORD_PROTECTED

    async def test_a_reset_holder_cannot_reset_a_super_admin_role_holder(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        super_role = await self._super_role(session, company)
        service = UserService(session, tenant)
        target = await service.create_user(
            principal, _payload(_email("root"), role_ids=[super_role.id])
        )
        helpdesk = _principal_with(principal, Permission.USER_RESET_PASSWORD)

        with pytest.raises(AuthorizationError) as excinfo:
            await service.reset_password(
                helpdesk, target.id, PasswordResetRequest(reason="Takeover")
            )
        self._assert_protected(excinfo)

        with pytest.raises(AuthorizationError) as excinfo:
            await service.update_user(helpdesk, target.id, UserUpdate(password=GOOD_PASSWORD))
        self._assert_protected(excinfo)

        stored = await session.get(User, target.id)
        assert stored is not None
        assert verify_password(GOOD_PASSWORD, stored.password_hash)

    async def test_a_reset_holder_cannot_reset_a_superuser_flag_holder(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        """The flag is the other route to the same authority; both are guarded."""
        service = UserService(session, tenant)
        target = await service.create_user(principal, _payload(_email("flag")))
        stored = await session.get(User, target.id)
        assert stored is not None
        stored.is_superuser = True
        await session.flush()

        helpdesk = _principal_with(principal, Permission.USER_RESET_PASSWORD)

        with pytest.raises(AuthorizationError) as excinfo:
            await service.reset_password(
                helpdesk, target.id, PasswordResetRequest(reason="Takeover")
            )
        self._assert_protected(excinfo)

        with pytest.raises(AuthorizationError) as excinfo:
            await service.update_user(helpdesk, target.id, UserUpdate(password=GOOD_PASSWORD))
        self._assert_protected(excinfo)

        assert verify_password(GOOD_PASSWORD, stored.password_hash)

    async def test_a_super_administrator_may_reset_another(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        super_role = await self._super_role(session, company)
        service = UserService(session, tenant)
        target = await service.create_user(
            principal, _payload(_email("root"), role_ids=[super_role.id])
        )

        by_role = Principal(
            user_id=principal.user_id,
            company_id=principal.company_id,
            email=principal.email,
            full_name=principal.full_name,
            is_superuser=False,
            permissions=frozenset({str(Permission.USER_RESET_PASSWORD)}),
            role_codes=(next(iter(SUPER_ADMIN_ROLE_CODES)),),
        )
        result = await service.reset_password(
            by_role, target.id, PasswordResetRequest(reason="Handover")
        )
        assert result.temporary_password is not None
        stored = await session.get(User, target.id)
        assert stored is not None
        assert verify_password(result.temporary_password, stored.password_hash)

        by_flag = Principal(
            user_id=principal.user_id,
            company_id=principal.company_id,
            email=principal.email,
            full_name=principal.full_name,
            is_superuser=True,
            permissions=frozenset(),
            role_codes=(),
        )
        await service.update_user(by_flag, target.id, UserUpdate(password="Chosen!Pass99xZ"))
        assert verify_password("Chosen!Pass99xZ", stored.password_hash)


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------


class TestReads:
    async def test_list_rows_batch_role_codes_and_do_not_load_the_relationship(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(
            principal, _payload(_email("grid"), role_ids=[buyer_role.id])
        )

        items, pagination = await service.list_users(
            UserFilters(search="grid"), PageParams(page=1, limit=50), SortParams()
        )
        row = next(item for item in items if item.id == created.id)
        assert row.role_codes == [buyer_role.code]
        assert pagination.total is not None

        # The relationship really is suppressed on a list statement: touching it
        # raises rather than quietly issuing a query per row.
        stmt = service.repo.build_list_stmt(UserFilters(), SortParams())
        loaded = (await session.execute(stmt)).scalars().unique().all()
        target = next(item for item in loaded if item.id == created.id)
        with pytest.raises(InvalidRequestError):
            _ = target.roles

    async def test_role_filter_finds_holders(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        holder = await service.create_user(principal, _payload(_email(), role_ids=[buyer_role.id]))
        await service.create_user(principal, _payload(_email()))

        items, _ = await service.list_users(
            UserFilters(role_id=buyer_role.id), PageParams(limit=50), SortParams()
        )
        assert [item.id for item in items] == [holder.id]

    async def test_effective_permissions_apply_denials_over_roles(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(
            principal,
            _payload(
                _email(),
                role_ids=[buyer_role.id],
                extra_permissions=[str(Permission.PAYMENT_VIEW)],
                denied_permissions=[str(Permission.PURCHASE_ORDER_CREATE)],
            ),
        )

        resolved = await service.get_effective_permissions(created.id)
        assert str(Permission.PURCHASE_ORDER_VIEW) in resolved.permissions
        assert str(Permission.PAYMENT_VIEW) in resolved.permissions
        assert str(Permission.PURCHASE_ORDER_CREATE) not in resolved.permissions
        assert resolved.role_permissions[buyer_role.code] == sorted(buyer_role.permissions)
        assert resolved.permission_count == len(resolved.permissions)

    async def test_a_suspended_role_contributes_nothing(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        service = UserService(session, tenant)
        created = await service.create_user(principal, _payload(_email(), role_ids=[buyer_role.id]))

        buyer_role.is_active = False
        await session.flush()

        resolved = await service.get_effective_permissions(created.id)
        assert resolved.permissions == []
        assert resolved.role_permissions == {}


# -----------------------------------------------------------------------------
# Roles
# -----------------------------------------------------------------------------


class TestRoles:
    async def test_creation_refuses_permissions_the_caller_lacks(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal
    ) -> None:
        weak = _principal_with(principal, Permission.ROLE_MANAGE, Permission.ROLE_VIEW)
        with pytest.raises(PermissionDeniedError):
            await RoleService(session, tenant).create_role(
                weak,
                RoleCreate(
                    code="ESCALATE",
                    name="Escalation",
                    permissions=[str(Permission.PAYMENT_APPROVE)],
                ),
            )

    async def test_duplicate_code_is_refused(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, buyer_role: Role
    ) -> None:
        with pytest.raises(DuplicateError):
            await RoleService(session, tenant).create_role(
                principal, RoleCreate(code=buyer_role.code, name="Clash")
            )

    async def test_system_roles_are_editable_but_super_admin_is_not(
        self, session: AsyncSession, tenant: TenantContext, principal: Principal, company
    ) -> None:
        editable = Role(
            company_id=company.id,
            code=f"SYSTEM_{uuid.uuid4().hex[:5].upper()}",
            name="A System Role",
            permissions=[str(Permission.ITEM_VIEW)],
            is_system=True,
            is_active=True,
        )
        immutable = Role(
            company_id=company.id,
            code=next(iter(SUPER_ADMIN_ROLE_CODES)),
            name="Super Administrator",
            permissions=[str(p) for p in Permission],
            is_system=True,
            is_active=True,
        )
        session.add_all([editable, immutable])
        await session.flush()

        service = RoleService(session, tenant)
        updated = await service.update_role(
            principal,
            editable.id,
            RoleUpdate(permissions=[str(Permission.ITEM_VIEW), str(Permission.BOM_VIEW)]),
        )
        assert sorted(updated.permissions) == sorted(
            [str(Permission.ITEM_VIEW), str(Permission.BOM_VIEW)]
        )

        with pytest.raises(BusinessRuleError):
            await service.update_role(principal, immutable.id, RoleUpdate(name="Renamed"))

    async def test_permission_edit_is_audited_with_the_affected_count(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        await UserService(session, tenant).create_user(
            principal, _payload(_email(), role_ids=[buyer_role.id])
        )

        await RoleService(session, tenant).update_role(
            principal, buyer_role.id, RoleUpdate(permissions=[str(Permission.ITEM_VIEW)])
        )

        rows = await _audit_rows(session, buyer_role.id, AuditAction.PERMISSION_CHANGE)
        assert len(rows) == 1
        assert rows[0].context is not None and rows[0].context["affected_user_count"] == 1
        assert rows[0].new_values == {"permissions": [str(Permission.ITEM_VIEW)]}

    async def test_a_system_role_cannot_be_deleted(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        vendor_role: Role,
    ) -> None:
        # It would be recreated by the seed on the next deployment, so the
        # "deletion" produces a role that keeps coming back.
        with pytest.raises(BusinessRuleError):
            await RoleService(session, tenant).delete_role(vendor_role.id, reason="Unwanted")

    async def test_a_held_role_cannot_be_deleted(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        principal: Principal,
        buyer_role: Role,
    ) -> None:
        await UserService(session, tenant).create_user(
            principal, _payload(_email(), role_ids=[buyer_role.id])
        )
        with pytest.raises(BusinessRuleError) as excinfo:
            await RoleService(session, tenant).delete_role(buyer_role.id, reason="Obsolete")
        assert excinfo.value.code is ErrorCode.REFERENCED_RECORD_IN_USE
        assert excinfo.value.details["user_count"] == 1

    async def test_an_unheld_role_is_soft_deleted(
        self, session: AsyncSession, tenant: TenantContext, buyer_role: Role
    ) -> None:
        await RoleService(session, tenant).delete_role(buyer_role.id, reason="Obsolete")
        stored = (await session.execute(select(Role).where(Role.id == buyer_role.id))).scalar_one()
        assert stored.deleted_at is not None

    async def test_list_reports_permission_counts_without_shipping_the_arrays(
        self, session: AsyncSession, tenant: TenantContext, buyer_role: Role
    ) -> None:
        items, _ = await RoleService(session, tenant).list_roles(
            RoleFilters(search=buyer_role.code), PageParams(limit=50), SortParams()
        )
        row = next(item for item in items if item.id == buyer_role.id)
        assert row.permission_count == len(buyer_role.permissions)

    async def test_grants_permission_filter_is_evaluated_in_sql(
        self, session: AsyncSession, tenant: TenantContext, buyer_role: Role, company
    ) -> None:
        unrelated = Role(
            company_id=company.id,
            code=f"QC_{uuid.uuid4().hex[:6].upper()}",
            name="QC",
            permissions=[str(Permission.QC_VIEW)],
            is_active=True,
        )
        session.add(unrelated)
        await session.flush()

        items, _ = await RoleService(session, tenant).list_roles(
            RoleFilters(grants_permission=str(Permission.PURCHASE_ORDER_CREATE)),
            PageParams(limit=50),
            SortParams(),
        )
        found = {item.id for item in items}
        assert buyer_role.id in found
        assert unrelated.id not in found
