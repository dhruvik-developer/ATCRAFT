"""Queries for the identity master.

Everything that touches the database lives here; the service decides *whether*
an operation is allowed, this file decides *how* it is read or written.

Five choices are worth reading twice:

* **The user list suppresses the ``roles`` relationship and batches the codes.**
  ``User.roles`` is ``lazy="selectin"`` because the authentication path resolves
  permissions on every request - but a grid does not need whole ``Role`` rows,
  so the list statement replaces that strategy with ``raiseload`` and the codes
  are fetched for the whole page in one join (section 32). ``raiseload`` rather
  than ``noload`` so a future edit that reaches for ``user.roles`` on a list row
  fails loudly instead of quietly reporting that nobody has any roles.
* **Role membership is written against the association table directly.**
  Assigning through ``user.roles`` cannot populate ``user_roles.assigned_by_id``,
  and "who granted this person payment approval" is the first question asked
  after an incident.
* **The last-super-administrator count is a single EXISTS query.** Loading every
  user and counting in Python would be both an unbounded read and a race: the
  guard has to be evaluated inside the same transaction as the change it
  protects.
* **List rows select an explicit column set.** ``users`` carries a password
  hash, an MFA secret, two UUID arrays and a JSONB blob; none of them belong in
  a grid, and ``raiseload=True`` makes touching an unloaded one an error rather
  than another query per row.
* **Cross-module reads are read-only.** ``branches``, ``plants``,
  ``departments``, ``vendors``, ``customers``, ``item_categories`` and
  ``vendor_categories``
  are owned by other modules; this file only probes them within the tenant -
  existence, and which categories a vendor is qualified for - and never writes
  to them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, delete, exists, func, insert, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute, load_only, raiseload, selectinload

from src.common.repository import BaseRepository
from src.core.clock import utcnow
from src.core.database import affected_rows
from src.core.models import Base
from src.core.pagination import SortParams

# Read-only references to master tables owned by other modules. A user is placed
# in an organisation and may be bound to a vendor or a dealer customer; this
# module never writes to either, it only refuses an id that does not belong to
# the tenant.
from src.modules.customers.models import Customer
from src.modules.organization.models import Branch, Department, Plant
from src.modules.products.models import ItemCategory
from src.modules.users.models import Role, User, UserSession, user_roles
from src.modules.users.schemas import RoleFilters, UserFilters
from src.modules.vendors.models import Vendor, VendorCategory

#: Columns a user list row renders. The password hash, MFA secret, scope arrays
#: and preferences blob are absent on purpose - see the module docstring.
USER_SUMMARY_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    User.email,
    User.full_name,
    User.employee_code,
    User.is_active,
    User.is_superuser,
    User.must_change_password,
    User.vendor_id,
    User.customer_id,
    User.branch_id,
    User.plant_id,
    User.department_id,
    User.last_login_at,
    User.locked_until,
    # The default list order and the pagination tiebreaker read this.
    User.created_at,
)

#: Columns of ``roles`` a summary row renders. ``permissions`` is excluded - the
#: count is computed in SQL, so a 200-element array is never shipped per row.
ROLE_SUMMARY_COLUMNS: tuple[InstrumentedAttribute[Any], ...] = (
    Role.code,
    Role.name,
    Role.description,
    Role.is_system,
    Role.is_vendor_role,
    Role.is_dealer_role,
    Role.is_active,
    Role.created_at,
)


class UserRepository(BaseRepository[User]):
    """Persistence for :class:`~src.modules.users.models.User`."""

    model = User
    searchable_fields = ("email", "full_name", "employee_code", "phone")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "email": User.email,
        "full_name": User.full_name,
        "employee_code": User.employee_code,
        "is_active": User.is_active,
        "last_login_at": User.last_login_at,
        "created_at": User.created_at,
        "updated_at": User.updated_at,
    }

    def _scoped(self, *, include_deleted: bool = False) -> Select[tuple[User]]:
        """Tenant filter plus the portal restrictions.

        A principal with ``vendor_id`` set is a supplier's own staff signing in
        to the portal. Without this clause they read the entire internal
        directory - every employee's name, email, employee code, roles,
        ``is_superuser`` and last sign-in - because the base scope filters on
        company alone, and a vendor's staff belong to the same company row as
        the buyer's. The dealer binding (``customer_id``) is restricted the
        same way (docs/assumptions.md §C3-1).

        A principal with ``vendor_id`` set is a supplier's own staff signing in
        to the portal. Without this clause they read the entire internal
        directory - every employee's name, email, employee code, roles,
        ``is_superuser`` and last sign-in - because the base scope filters on
        company alone, and a vendor's staff belong to the same company row as
        the buyer's.

        :class:`~src.modules.vendors.repository.VendorRepository` has carried
        exactly this override from the start, and its docstring gives the reason
        this one now exists too: "Applied here rather than in each endpoint so no
        caller can forget it (section 16)." Every endpoint reading users had
        forgotten it.
        """
        stmt = super()._scoped(include_deleted=include_deleted)
        if self.tenant.vendor_id is not None:
            stmt = stmt.where(User.vendor_id == self.tenant.vendor_id)
        if self.tenant.customer_id is not None:
            stmt = stmt.where(User.customer_id == self.tenant.customer_id)
        return stmt

    # --- reads --------------------------------------------------------------

    def build_list_stmt(self, filters: UserFilters, sort: SortParams) -> Select[Any]:
        """The user list query."""
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.is_active is not None:
            stmt = stmt.where(User.is_active == filters.is_active)
        if filters.is_superuser is not None:
            stmt = stmt.where(User.is_superuser == filters.is_superuser)
        if filters.is_vendor_user is not None:
            stmt = stmt.where(
                User.vendor_id.is_not(None) if filters.is_vendor_user else User.vendor_id.is_(None)
            )
        if filters.vendor_id is not None:
            stmt = stmt.where(User.vendor_id == filters.vendor_id)
        if filters.is_dealer_user is not None:
            stmt = stmt.where(
                User.customer_id.is_not(None)
                if filters.is_dealer_user
                else User.customer_id.is_(None)
            )
        if filters.customer_id is not None:
            stmt = stmt.where(User.customer_id == filters.customer_id)
        if filters.branch_id is not None:
            stmt = stmt.where(User.branch_id == filters.branch_id)
        if filters.plant_id is not None:
            stmt = stmt.where(User.plant_id == filters.plant_id)
        if filters.department_id is not None:
            stmt = stmt.where(User.department_id == filters.department_id)

        if filters.is_locked is not None:
            # A lockout that has already expired is not a lockout, so the filter
            # compares against "now" rather than testing the column for NULL.
            locked = User.locked_until.is_not(None) & (User.locked_until > utcnow())
            stmt = stmt.where(locked if filters.is_locked else ~locked)

        if filters.role_id is not None:
            # EXISTS rather than a join: a user must appear once even though the
            # membership table is a many-to-many.
            stmt = stmt.where(
                exists().where(
                    user_roles.c.user_id == User.id,
                    user_roles.c.role_id == filters.role_id,
                )
            )

        stmt = stmt.options(
            load_only(*USER_SUMMARY_COLUMNS, raiseload=True),
            # Overrides the mapper's lazy="selectin": a grid does not need whole
            # Role rows, and the codes are batched separately.
            raiseload(User.roles),
        )

        default_order = sort.resolve(self.sortable_fields, User.full_name.asc())
        return stmt.order_by(default_order, User.id.asc())

    async def get_with_roles(
        self, user_id: uuid.UUID, *, include_deleted: bool = False
    ) -> User | None:
        """One user with its roles loaded.

        ``populate_existing`` is required, not decoration: the service mutates
        role membership through the association table, so an instance already in
        the identity map would otherwise hand back the collection as it looked
        before that write.
        """
        stmt = (
            self._scoped(include_deleted=include_deleted)
            .where(User.id == user_id)
            .options(selectinload(User.roles))
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def email_taken(self, email: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this address already used in this company, retired accounts included?

        Retired rows count. ``uq_users_company_id_email`` does not exclude
        soft-deleted rows, so allowing a reuse here would surface as an integrity
        error at flush time rather than as a clear message.
        """
        stmt = select(User.id).where(
            User.company_id == self.tenant.company_id,
            User.email == email,
        )
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def employee_code_taken(
        self, employee_code: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        """Is another live account already using this employee code?

        Not a database constraint - the column is nullable and contractors are
        often issued codes late - so the uniqueness that payroll reconciliation
        depends on is enforced here, against live rows only.
        """
        stmt = select(User.id).where(
            User.company_id == self.tenant.company_id,
            User.employee_code == employee_code,
            User.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def role_codes_for_users(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        """Role codes for a whole page of users, in one statement.

        The answer to a loop of single reads (section 32). Retired roles are
        excluded because they are no longer part of anybody's authority;
        *inactive* ones are included, because the grid should still show that
        the assignment exists.
        """
        if not user_ids:
            return {}

        stmt = (
            select(user_roles.c.user_id, Role.code)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(
                user_roles.c.user_id.in_(list(user_ids)),
                Role.company_id == self.tenant.company_id,
                Role.deleted_at.is_(None),
            )
            .order_by(user_roles.c.user_id, Role.code)
        )

        codes: dict[uuid.UUID, list[str]] = {}
        for user_id, code in (await self.session.execute(stmt)).all():
            codes.setdefault(user_id, []).append(code)
        return codes

    async def count_active_super_admins(
        self, role_codes: Sequence[str], *, exclude_user_id: uuid.UUID | None = None
    ) -> int:
        """How many live, active accounts still hold platform administration?

        Counts both routes to that authority: the ``is_superuser`` flag and
        membership of a usable super-administrator role. ``exclude_user_id``
        answers the question the guard actually asks - "would anybody be left if
        this one went away?" - without a second round trip.
        """
        holds_role = (
            select(user_roles.c.user_id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(
                user_roles.c.user_id == User.id,
                Role.company_id == self.tenant.company_id,
                Role.code.in_(list(role_codes)),
                Role.is_active.is_(True),
                Role.deleted_at.is_(None),
            )
        )

        stmt = (
            select(func.count())
            .select_from(User)
            .where(
                User.company_id == self.tenant.company_id,
                User.deleted_at.is_(None),
                User.is_active.is_(True),
                or_(User.is_superuser.is_(True), holds_role.exists()),
            )
        )
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def user_ids_with_role(self, role_id: uuid.UUID) -> list[uuid.UUID]:
        """Every account holding this role, live ones only.

        Used to invalidate the cached permission set of each affected user after
        a role's permissions change. Retired accounts are skipped: they cannot
        authenticate, so they have no cache entry to drop.
        """
        stmt = (
            select(user_roles.c.user_id)
            .join(User, User.id == user_roles.c.user_id)
            .where(
                user_roles.c.role_id == role_id,
                User.company_id == self.tenant.company_id,
                User.deleted_at.is_(None),
            )
        )
        return [row[0] for row in (await self.session.execute(stmt)).all()]

    async def count_users_with_role(self, role_id: uuid.UUID) -> int:
        """Live holders of a role - the "12 users will be affected" figure."""
        stmt = (
            select(func.count())
            .select_from(user_roles)
            .join(User, User.id == user_roles.c.user_id)
            .where(
                user_roles.c.role_id == role_id,
                User.company_id == self.tenant.company_id,
                User.deleted_at.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # --- membership writes --------------------------------------------------

    async def replace_roles(
        self,
        user_id: uuid.UUID,
        role_ids: Sequence[uuid.UUID],
        *,
        assigned_by_id: uuid.UUID | None,
    ) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Make the user's membership exactly ``role_ids``. Returns (added, removed).

        Written against ``user_roles`` rather than through ``User.roles`` so
        ``assigned_by_id`` is recorded. The table has no ``company_id`` of its
        own; tenant safety comes from the caller, which must have loaded both the
        user and every role through a tenant-scoped query first.

        Rows that are already correct are left untouched, so re-saving an
        unchanged checklist does not rewrite ``assigned_at`` and lose the date
        the authority was actually granted.
        """
        target = set(role_ids)

        current_stmt = select(user_roles.c.role_id).where(user_roles.c.user_id == user_id)
        current = {row[0] for row in (await self.session.execute(current_stmt)).all()}

        removed = current - target
        added = target - current

        if removed:
            await self.session.execute(
                delete(user_roles).where(
                    user_roles.c.user_id == user_id,
                    user_roles.c.role_id.in_(sorted(removed)),
                )
            )
        if added:
            await self.session.execute(
                insert(user_roles),
                [
                    {"user_id": user_id, "role_id": role_id, "assigned_by_id": assigned_by_id}
                    for role_id in sorted(added)
                ],
            )

        await self.session.flush()
        return added, removed

    # --- cross-module existence probes -------------------------------------

    async def _reference_exists(self, model: type[Base], entity_id: uuid.UUID) -> bool:
        """Does this master record exist, live, in the caller's tenant?

        A foreign key alone would allow pointing a user at another company's
        branch, because ``branches.id`` is globally unique - the tenant filter is
        what makes the reference safe (section 40).
        """
        stmt = select(
            exists().where(
                model.id == entity_id,  # type: ignore[attr-defined]
                model.company_id == self.tenant.company_id,  # type: ignore[attr-defined]
                model.deleted_at.is_(None),  # type: ignore[attr-defined]
            )
        )
        return bool((await self.session.execute(stmt)).scalar())

    async def branch_exists(self, branch_id: uuid.UUID) -> bool:
        return await self._reference_exists(Branch, branch_id)

    async def plant_exists(self, plant_id: uuid.UUID) -> bool:
        return await self._reference_exists(Plant, plant_id)

    async def department_exists(self, department_id: uuid.UUID) -> bool:
        return await self._reference_exists(Department, department_id)

    async def vendor_exists(self, vendor_id: uuid.UUID) -> bool:
        return await self._reference_exists(Vendor, vendor_id)

    async def dealer_customer(self, customer_id: uuid.UUID) -> Customer | None:
        """The bound customer, tenant-scoped. The kind check lives in the service.

        Returned whole rather than as a boolean because the service needs the
        code and the type for the refusal message; one read serves both.
        """
        stmt = select(Customer).where(
            Customer.company_id == self.tenant.company_id,
            Customer.id == customer_id,
            Customer.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def live_category_ids(self, category_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these item categories exist, live, in the caller's tenant."""
        if not category_ids:
            return set()
        stmt = select(ItemCategory.id).where(
            ItemCategory.company_id == self.tenant.company_id,
            ItemCategory.id.in_(list(dict.fromkeys(category_ids))),
            ItemCategory.deleted_at.is_(None),
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def vendor_category_ids(self, vendor_id: uuid.UUID) -> set[uuid.UUID]:
        """The item categories a vendor is qualified for (``vendor_categories``)."""
        stmt = select(VendorCategory.item_category_id).where(
            VendorCategory.company_id == self.tenant.company_id,
            VendorCategory.vendor_id == vendor_id,
        )
        return set((await self.session.execute(stmt)).scalars().all())


class RoleRepository(BaseRepository[Role]):
    """Persistence for :class:`~src.modules.users.models.Role`."""

    model = Role
    searchable_fields = ("code", "name", "description")
    sortable_fields: dict[str, InstrumentedAttribute[Any]] = {  # noqa: RUF012
        "code": Role.code,
        "name": Role.name,
        "is_system": Role.is_system,
        "is_vendor_role": Role.is_vendor_role,
        "is_dealer_role": Role.is_dealer_role,
        "is_active": Role.is_active,
        "created_at": Role.created_at,
        "updated_at": Role.updated_at,
    }

    def build_list_stmt(self, filters: RoleFilters, sort: SortParams) -> Select[Any]:
        stmt = self._scoped(include_deleted=filters.include_deleted)
        stmt = self._apply_search(stmt, filters.search)

        if filters.is_system is not None:
            stmt = stmt.where(Role.is_system == filters.is_system)
        if filters.is_vendor_role is not None:
            stmt = stmt.where(Role.is_vendor_role == filters.is_vendor_role)
        if filters.is_dealer_role is not None:
            stmt = stmt.where(Role.is_dealer_role == filters.is_dealer_role)
        if filters.is_active is not None:
            stmt = stmt.where(Role.is_active == filters.is_active)
        if filters.grants_permission is not None:
            # PostgreSQL array containment, evaluated in the database. Fetching
            # every role and filtering in Python would page incorrectly - the
            # rows removed after the LIMIT leave short pages and skipped roles.
            stmt = stmt.where(Role.permissions.contains([filters.grants_permission]))

        # The permission array is not loaded for list rows; the count comes from
        # cardinality() in the summary query instead.
        stmt = stmt.options(load_only(*ROLE_SUMMARY_COLUMNS, raiseload=True), raiseload(Role.users))

        default_order = sort.resolve(self.sortable_fields, Role.code.asc())
        return stmt.order_by(default_order, Role.id.asc())

    async def get_by_code(self, code: str, *, include_deleted: bool = False) -> Role | None:
        stmt = self._scoped(include_deleted=include_deleted).where(Role.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_taken(self, code: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Is this code in use, retired roles included?

        ``uq_roles_company_id_code`` covers soft-deleted rows too, so reusing the
        code of a retired role would fail at flush time.
        """
        stmt = select(Role.id).where(Role.company_id == self.tenant.company_id, Role.code == code)
        if exclude_id is not None:
            stmt = stmt.where(Role.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def get_assignable(self, role_ids: Sequence[uuid.UUID]) -> list[Role]:
        """Load roles that may be assigned, scoped to the caller's tenant.

        Returns only what it found. The service compares the result against what
        was asked for and reports the missing ids - which is also, structurally,
        the enforcement of "a user may only be assigned roles from their own
        company": another tenant's role simply is not in the result.
        """
        if not role_ids:
            return []
        stmt = self._scoped().where(Role.id.in_(list(role_ids)))
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def permission_counts(self, role_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Permission-array cardinality for a page of roles, in one statement.

        Computed in PostgreSQL so the arrays themselves never cross the wire for
        a list view.
        """
        if not role_ids:
            return {}
        stmt = select(Role.id, func.coalesce(func.cardinality(Role.permissions), 0)).where(
            Role.company_id == self.tenant.company_id, Role.id.in_(list(role_ids))
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}


class UserSessionRepository(BaseRepository[UserSession]):
    """Persistence for refresh-token sessions.

    This module owns only the revocation side. Issuing and rotating sessions
    belongs to ``auth``; user administration merely needs to be able to end them
    when an account is deactivated, retired or has its password reset.
    """

    model = UserSession

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, reason: str) -> int:
        """End every live session of one user. Returns how many were revoked.

        One UPDATE, not a read-then-write loop: a session that is created
        between the read and the write would otherwise survive the revocation.
        """
        from src.modules.notifications.push_repository import remove_user_push_devices

        await remove_user_push_devices(self.session, self.tenant.company_id, user_id)
        stmt = (
            update(UserSession)
            .where(
                UserSession.company_id == self.tenant.company_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow(), revoked_reason=reason[:64])
            .execution_options(synchronize_session=False)
        )
        return affected_rows(await self.session.execute(stmt))

    async def count_live_for_user(self, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(UserSession)
            .where(
                UserSession.company_id == self.tenant.company_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())


__all__ = [
    "ROLE_SUMMARY_COLUMNS",
    "USER_SUMMARY_COLUMNS",
    "RoleRepository",
    "UserRepository",
    "UserSessionRepository",
]
