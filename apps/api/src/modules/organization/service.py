"""Business rules for the organisation masters.

Rules enforced here, and the reason each one exists:

* **The company endpoint can only ever reach the caller's own company.** The
  tenant boundary is not a filter that a future refactor could drop - the
  repository's base statement *is* ``companies.id = <principal's company>``, so
  there is no query shape in this module that could return another tenant's row.
* **A code is never reused**, not even one belonging to a retired record: it is
  quoted on historical documents, so a second branch answering to ``HO`` makes
  the archive ambiguous.
* **The department tree stays a tree.** A cycle in ``parent_id`` turns every
  roll-up and every approval-routing walk into an infinite loop, so a parent
  that is already a descendant is refused before it is written.
* **At most one head office.** The nomination is refused rather than silently
  moved: which location is the registered office decides where statutory
  correspondence goes, and quietly reassigning it is not a decision this API
  should make on the user's behalf.
* **Active financial years never overlap.** Two open periods covering the same
  date make "which year does this posting belong to" unanswerable, and document
  numbering is scoped to the year.
* **Closing a financial year is one-way for everyone but a platform
  administrator**, and both the close and any reopen are audited.
* **Retirement is refused while anything still points at the record** - a user,
  a warehouse, a plant, a work centre, an order or a child department. Retiring
  it anyway would leave those rows pointing at something the UI no longer lists.
* **A cost centre is optional everywhere and validated wherever it is named.**
  A foreign key to a UUID is satisfied by another company's row, so every
  module that accepts one asks :func:`require_cost_centre` (or the batch
  :func:`require_cost_centres`) first. Posted ledger history keeps its centre
  after retirement; only a *new* allocation needs it to be active.

Every state change goes through a :class:`~src.core.state_machine.StateMachine`
and is written to the audit log inside the same transaction as the change.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.common.banking import mask_account_number
from src.common.codes import financial_year_key
from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    NotFoundError,
    ValidationError,
)
from src.core.messages import NO_FIELDS_TO_UPDATE, inactive_record
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.organization.models import (
    Branch,
    Company,
    CostCentre,
    Department,
    FinancialYear,
    Plant,
)
from src.modules.organization.repository import (
    BranchRepository,
    CompanyRepository,
    CostCentreFacts,
    CostCentreRepository,
    DepartmentRepository,
    FinancialYearRepository,
    PlantRepository,
)
from src.modules.organization.schemas import (
    BranchCreate,
    BranchFilters,
    BranchRead,
    BranchUpdate,
    CompanyRead,
    CompanyUpdate,
    CostCentreCreate,
    CostCentreFilters,
    CostCentreRead,
    CostCentreUpdate,
    DepartmentCreate,
    DepartmentFilters,
    DepartmentRead,
    DepartmentUpdate,
    FinancialYearCreate,
    FinancialYearFilters,
    FinancialYearRead,
    FinancialYearUpdate,
    PlantCreate,
    PlantFilters,
    PlantRead,
    PlantUpdate,
)

AUDIT_ENTITY_COMPANY = "company"
AUDIT_ENTITY_BRANCH = "branch"
AUDIT_ENTITY_PLANT = "plant"
AUDIT_ENTITY_DEPARTMENT = "department"
AUDIT_ENTITY_COST_CENTRE = "cost_centre"
AUDIT_ENTITY_FINANCIAL_YEAR = "financial_year"

#: How a cost centre is named in messages and on its state machine.
COST_CENTRE_ENTITY = "Cost centre"

# ASSUMPTION: a department hierarchy deeper than six levels is a data-entry
# mistake rather than a real org chart - company > division > function >
# department > section > cell is already six. A named constant rather than a
# database constraint, so raising it is a deliberate code change and not a
# migration.
MAX_DEPARTMENT_DEPTH = 6

# ASSUMPTION: a cost-centre tree is bounded exactly like the department tree it
# usually shadows - a plant > production > process > cell breakdown is four,
# leaving room for two more. Equal to MAX_DEPARTMENT_DEPTH by definition rather
# than by coincidence, so the two hierarchies cannot drift apart unnoticed; see
# docs/assumptions.md §B1-5. Unlike the department rule, the depth counted is
# the deepest descendant's, so a subtree cannot be reparented past the limit.
MAX_COST_CENTRE_DEPTH = MAX_DEPARTMENT_DEPTH

# ASSUMPTION: a financial year longer than 550 days (roughly 18 months) is a
# typo. The Companies Act allows a first financial period of up to 15 months,
# extendable to 18 with registrar approval, so 550 days accommodates the longest
# legitimate case while still catching a mistyped century.
MAX_FINANCIAL_YEAR_DAYS = 550

# ASSUMPTION: reopening a closed financial year is restricted to a platform
# administrator (``Principal.is_superuser``) rather than to a permission string.
# There is no ``financial_year.reopen`` member in the Permission enum, and
# minting one here would let it be granted through the ordinary role editor -
# which is precisely what "irreversible without SUPER_ADMIN" rules out.

#: Fields captured before and after an edit so the audit entry records exactly
#: what changed. Explicit rather than "every column": ``created_at`` and the
#: actor columns are not business changes.
_COMPANY_AUDITED_FIELDS = [
    "name",
    "legal_name",
    "gstin",
    "pan",
    "cin",
    "email",
    "phone",
    "website",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "currency",
    "financial_year_start_month",
    "timezone",
    "logo_document_id",
    "bank_name",
    "bank_account_name",
    "bank_account_number",
    "bank_ifsc",
    "bank_branch",
    "invoice_terms",
]

_BRANCH_AUDITED_FIELDS = [
    "code",
    "name",
    "gstin",
    "address_line1",
    "city",
    "state",
    "state_code",
    "postal_code",
    "phone",
    "email",
    "is_active",
    "is_head_office",
]

_PLANT_AUDITED_FIELDS = [
    "code",
    "name",
    "branch_id",
    "address_line1",
    "city",
    "state",
    "is_active",
]

_DEPARTMENT_AUDITED_FIELDS = [
    "code",
    "name",
    "plant_id",
    "parent_id",
    "head_user_id",
    "cost_centre_id",
    "is_active",
]

_COST_CENTRE_AUDITED_FIELDS = [
    "code",
    "name",
    "cost_centre_type",
    "parent_id",
    "plant_id",
    "head_user_id",
    "description",
    "is_active",
]

#: Columns a cost-centre PATCH may not null. An explicit ``null`` for one of
#: these is a client mistake, not an instruction, and is ignored rather than
#: written into a NOT NULL column as an integrity error naming a constraint.
_COST_CENTRE_REQUIRED_FIELDS = frozenset({"name", "cost_centre_type", "is_active"})

_FINANCIAL_YEAR_AUDITED_FIELDS = [
    "code",
    "start_date",
    "end_date",
    "is_active",
    "is_closed",
    "notes",
]


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class MasterLifecycleState(StrEnum):
    """Lifecycle of an organisation master record.

    There is no ``status`` column: the state is derived from ``is_active`` and
    ``deleted_at``, the two flags the rest of the system already reads. A third,
    redundant column could disagree with them. Routing the transitions through a
    :class:`StateMachine` still gives one place where the legal moves are
    declared, tested and rendered for the UI (section 18).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


def _lifecycle(entity: str, manage: Permission) -> StateMachine[MasterLifecycleState]:
    """Activate / deactivate / retire, with retirement terminal.

    ARCHIVED is terminal on purpose: this module exposes no restore endpoint, so
    declaring an edge out of it would advertise a transition no caller can make.
    """
    return StateMachine(
        entity=entity,
        initial=MasterLifecycleState.ACTIVE,
        transitions=(
            Transition(
                MasterLifecycleState.ACTIVE,
                MasterLifecycleState.INACTIVE,
                permission=manage,
                label="Deactivate",
            ),
            Transition(
                MasterLifecycleState.INACTIVE,
                MasterLifecycleState.ACTIVE,
                permission=manage,
                label="Activate",
            ),
            Transition(
                MasterLifecycleState.ACTIVE,
                MasterLifecycleState.ARCHIVED,
                permission=manage,
                label="Retire",
                is_irreversible=True,
            ),
            Transition(
                MasterLifecycleState.INACTIVE,
                MasterLifecycleState.ARCHIVED,
                permission=manage,
                label="Retire",
                is_irreversible=True,
            ),
        ),
        terminal=frozenset({MasterLifecycleState.ARCHIVED}),
    )


#: The same factory under a public name, for the master tables other modules own
#: (the brand and manufacturer masters in ``products``). One definition of
#: "activate, deactivate, retire" rather than a copy per module: a copy is how
#: two masters end up disagreeing about whether a retired row may come back.
master_lifecycle = _lifecycle


BRANCH_LIFECYCLE = _lifecycle("Branch", Permission.BRANCH_MANAGE)
PLANT_LIFECYCLE = _lifecycle("Plant", Permission.PLANT_MANAGE)
DEPARTMENT_LIFECYCLE = _lifecycle("Department", Permission.DEPARTMENT_MANAGE)
COST_CENTRE_LIFECYCLE = _lifecycle(COST_CENTRE_ENTITY, Permission.COST_CENTRE_MANAGE)
FINANCIAL_YEAR_LIFECYCLE = _lifecycle("Financial year", Permission.SETTINGS_MANAGE)


class FinancialYearState(StrEnum):
    """Posting state of an accounting period."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


#: Closing is marked irreversible because for every ordinary user it is: the
#: reopen edge exists so a platform administrator can undo a mistake, and the
#: service refuses it to anyone else.
FINANCIAL_YEAR_MACHINE: StateMachine[FinancialYearState] = StateMachine(
    entity="Financial year",
    initial=FinancialYearState.OPEN,
    transitions=(
        Transition(
            FinancialYearState.OPEN,
            FinancialYearState.CLOSED,
            permission=Permission.SETTINGS_MANAGE,
            label="Close year",
            is_irreversible=True,
        ),
        Transition(
            FinancialYearState.CLOSED,
            FinancialYearState.OPEN,
            permission=None,
            label="Reopen year (platform administrator only)",
        ),
    ),
)


def lifecycle_state(
    record: Branch | Plant | Department | CostCentre | FinancialYear,
) -> MasterLifecycleState:
    """Derive the lifecycle state of a master row from its flags."""
    if record.deleted_at is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if record.is_active else MasterLifecycleState.INACTIVE


def financial_year_state(year: FinancialYear) -> FinancialYearState:
    return FinancialYearState.CLOSED if year.is_closed else FinancialYearState.OPEN


# -----------------------------------------------------------------------------
# Pure rule helpers - unit tested without a database
# -----------------------------------------------------------------------------


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot retire this, something still points at it" error.

    One constructor so every such refusal carries the same machine-readable code
    and the frontend can react to it uniformly.
    """
    return BusinessRuleError(
        message, code=ErrorCode.REFERENCED_RECORD_IN_USE, details=details or None
    )


def assert_parent_allowed(
    *,
    entity: str,
    node_id: uuid.UUID | None,
    parent_id: uuid.UUID,
    parent_ancestor_ids: list[uuid.UUID],
    max_depth: int = MAX_DEPARTMENT_DEPTH,
    subtree_height: int = 0,
) -> None:
    """Reject a parent assignment that would break the tree.

    ``parent_ancestor_ids`` is the chain from the proposed parent up to its root,
    *including the parent itself*, as returned by the repository's recursive
    walk. ``subtree_height`` is how many levels sit *below* the record - zero
    for a leaf, and always zero on a create. Three failures are possible:

    * the record is its own parent;
    * the record already appears above the proposed parent, which would close a
      cycle - the case that turns a roll-up query into an infinite loop;
    * the resulting chain would be deeper than ``max_depth``, counted to the
      deepest descendant that moves with the record.
    """
    if node_id is not None and parent_id == node_id:
        raise BusinessRuleError(f"A {entity} cannot be its own parent.")

    if node_id is not None and node_id in parent_ancestor_ids:
        raise BusinessRuleError(
            f"That parent is already below this {entity}; the hierarchy must stay a tree.",
            details={"parent_id": str(parent_id)},
        )

    # +1 for the record itself, which sits one level under the proposed parent,
    # and the levels beneath it, which move with it.
    depth = len(parent_ancestor_ids) + 1 + max(0, subtree_height)
    if depth > max_depth:
        raise BusinessRuleError(
            f"{entity.capitalize()} hierarchy may not be deeper than {max_depth} levels.",
            details={"depth": depth, "max_depth": max_depth},
        )


def ranges_overlap(
    first_start: date, first_end: date, second_start: date, second_end: date
) -> bool:
    """Do two inclusive date ranges intersect?

    Both endpoints belong to the period - 31 March is inside the year that ends
    on it - so the comparison is ``<=`` at both ends. Sharing a single day is an
    overlap: a posting on that day would belong to two years.
    """
    return first_start <= second_end and second_start <= first_end


def assert_period_valid(start_date: date, end_date: date) -> None:
    """Validate the shape of a financial period in isolation.

    Separate from the overlap check because this one needs nothing but the two
    dates, which makes it exhaustively testable without a database.
    """
    if start_date >= end_date:
        raise BusinessRuleError(
            "A financial year must start before it ends.",
            details={"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    span_days = (end_date - start_date).days + 1
    if span_days > MAX_FINANCIAL_YEAR_DAYS:
        raise BusinessRuleError(
            f"A financial year may not span more than {MAX_FINANCIAL_YEAR_DAYS} days.",
            details={"days": span_days, "max_days": MAX_FINANCIAL_YEAR_DAYS},
        )


# -----------------------------------------------------------------------------
# Company
# -----------------------------------------------------------------------------


class CompanyService:
    """Read and edit the caller's own company. There is no other company here."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = CompanyRepository(session, tenant)

    async def get_company(self) -> CompanyRead:
        return CompanyRead.model_validate(await self._load())

    async def update_company(self, payload: CompanyUpdate) -> CompanyRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            company = await self._load()
            before = _company_snapshot(company)
            account_before = company.bank_account_number

            for field, value in changes.items():
                setattr(company, field, value)
            # No ``touch`` here: ``companies`` carries no actor columns - it
            # predates every user in it - so the actor lives on the audit row.
            await self.session.flush()

            # Two account numbers ending in the same four digits mask alike, so
            # the diff alone could not show that the payee account moved. Say
            # so explicitly, without writing either number down.
            account_changed = company.bank_account_number != account_before

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                # CONFIG_CHANGE rather than UPDATE: these fields drive tax
                # identity, currency and the financial calendar, so they read
                # differently in an audit review than an ordinary master edit.
                action=AuditAction.CONFIG_CHANGE,
                entity_type=AUDIT_ENTITY_COMPANY,
                entity_id=company.id,
                entity_code=company.code,
                summary=f"Updated company profile {company.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=_company_snapshot(company),
                context={"bank_account_number_changed": True} if account_changed else None,
            )

        return CompanyRead.model_validate(company)

    async def _load(self) -> Company:
        company = await self.repo.get_current()
        if company is None:
            # Only reachable if the tenant row was removed while a token for it
            # was still live. Reported as "not found" rather than as a server
            # error: from the caller's side the company genuinely is not there.
            raise NotFoundError("Company", self.tenant.company_id)
        return company


def _company_snapshot(company: Company) -> dict[str, Any]:
    """Audited company fields, with the bank account number masked.

    The account customers pay into is printed on every invoice, so editing it
    is how invoice-redirection fraud starts: the audit row must prove the
    number changed without becoming a second copy of it - the rule the vendor
    master already applies to the accounts the company pays out to.
    """
    captured = snapshot(company, _COMPANY_AUDITED_FIELDS)
    captured["bank_account_number"] = mask_account_number(captured["bank_account_number"])
    return captured


# -----------------------------------------------------------------------------
# Branch
# -----------------------------------------------------------------------------


class BranchService:
    """Branch master-data operations. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = BranchRepository(session, tenant)

    async def list_branches(
        self, filters: BranchFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[BranchRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [BranchRead.model_validate(row) for row in rows], pagination

    async def get_branch(self, branch_id: uuid.UUID) -> BranchRead:
        return BranchRead.model_validate(await self.repo.get_or_404(branch_id))

    async def create_branch(self, payload: BranchCreate) -> BranchRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Branch code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            if payload.is_head_office:
                await self._assert_head_office_free(exclude_id=None)

            branch = Branch(**payload.model_dump())
            self.repo.add(branch)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_BRANCH,
                entity_id=branch.id,
                entity_code=branch.code,
                summary=f"Created branch {branch.code} ({branch.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(branch, _BRANCH_AUDITED_FIELDS),
            )

        return BranchRead.model_validate(branch)

    async def update_branch(self, branch_id: uuid.UUID, payload: BranchUpdate) -> BranchRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            branch = await self.repo.get_or_404(branch_id)
            before = snapshot(branch, _BRANCH_AUDITED_FIELDS)
            current_state = lifecycle_state(branch)

            if changes.get("is_head_office") and not branch.is_head_office:
                await self._assert_head_office_free(exclude_id=branch.id)

            target_state = self._validate_activation(branch, changes, current_state)

            for field, value in changes.items():
                setattr(branch, field, value)
            self.repo.touch(branch)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_BRANCH,
                entity_id=branch.id,
                entity_code=branch.code,
                summary=f"Updated branch {branch.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(branch, _BRANCH_AUDITED_FIELDS),
            )
            if target_state is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ENTITY_BRANCH,
                    entity_id=branch.id,
                    entity_code=branch.code,
                    from_status=str(current_state),
                    to_status=str(target_state),
                    user_id=self.tenant.user_id,
                    reason="Activation flag changed through branch update",
                )

        return BranchRead.model_validate(branch)

    async def delete_branch(self, branch_id: uuid.UUID, *, reason: str) -> None:
        """Retire a branch (soft delete), refusing while anything still needs it."""
        async with transaction(self.session):
            branch = await self.repo.get_or_404(branch_id)
            current = lifecycle_state(branch)
            BRANCH_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if branch.is_head_office:
                raise referenced_in_use(
                    "The head office cannot be retired. Nominate another branch first.",
                    branch_code=branch.code,
                )
            if await self.repo.referenced_by_users(branch.id):
                raise referenced_in_use(
                    f"Branch {branch.code} is still assigned to one or more users.",
                    branch_code=branch.code,
                )
            if await self.repo.referenced_by_plants(branch.id):
                raise referenced_in_use(
                    f"Branch {branch.code} still has plants. Reassign or retire those first.",
                    branch_code=branch.code,
                )
            if await self.repo.referenced_by_warehouses(branch.id):
                raise referenced_in_use(
                    f"Branch {branch.code} still has warehouses. Reassign those first.",
                    branch_code=branch.code,
                )

            branch.is_active = False
            await self.repo.soft_delete(branch)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_BRANCH,
                entity_id=branch.id,
                entity_code=branch.code,
                summary=f"Retired branch {branch.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    async def _assert_head_office_free(self, *, exclude_id: uuid.UUID | None) -> None:
        """Refuse a second head office rather than silently demoting the first.

        Which location is the registered office decides where statutory notices
        are served; moving it is a decision the user must make explicitly, by
        clearing the flag on the current head office first.
        """
        existing = await self.repo.head_office_id(exclude_id=exclude_id)
        if existing is not None:
            raise BusinessRuleError(
                "This company already has a head office. Clear the flag on it first.",
                details={"head_office_branch_id": str(existing)},
            )

    def _validate_activation(
        self,
        branch: Branch,
        changes: dict[str, Any],
        current: MasterLifecycleState,
    ) -> MasterLifecycleState | None:
        """Route an ``is_active`` edit through the lifecycle machine.

        Returns the target state when the flag actually changes, so the caller
        knows to write a status-change audit row as well as the field diff.
        """
        if "is_active" not in changes or changes["is_active"] == branch.is_active:
            return None
        target = (
            MasterLifecycleState.ACTIVE if changes["is_active"] else MasterLifecycleState.INACTIVE
        )
        BRANCH_LIFECYCLE.assert_transition(current, target)
        if target is MasterLifecycleState.INACTIVE and branch.is_head_office:
            raise BusinessRuleError(
                "The head office cannot be deactivated. Nominate another head office first.",
                details={"branch_code": branch.code},
            )
        return target


# -----------------------------------------------------------------------------
# Plant
# -----------------------------------------------------------------------------


class PlantService:
    """Plant master-data operations."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = PlantRepository(session, tenant)

    async def list_plants(
        self, filters: PlantFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[PlantRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [PlantRead.model_validate(row) for row in rows], pagination

    async def get_plant(self, plant_id: uuid.UUID) -> PlantRead:
        return PlantRead.model_validate(await self.repo.get_or_404(plant_id))

    async def create_plant(self, payload: PlantCreate) -> PlantRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Plant code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            await self._assert_branch(payload.branch_id)

            plant = Plant(**payload.model_dump())
            self.repo.add(plant)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_PLANT,
                entity_id=plant.id,
                entity_code=plant.code,
                summary=f"Created plant {plant.code} ({plant.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(plant, _PLANT_AUDITED_FIELDS),
            )

        return PlantRead.model_validate(plant)

    async def update_plant(self, plant_id: uuid.UUID, payload: PlantUpdate) -> PlantRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            plant = await self.repo.get_or_404(plant_id)
            before = snapshot(plant, _PLANT_AUDITED_FIELDS)
            current_state = lifecycle_state(plant)

            if "branch_id" in changes and changes["branch_id"] != plant.branch_id:
                await self._assert_branch(changes["branch_id"])

            target_state: MasterLifecycleState | None = None
            if "is_active" in changes and changes["is_active"] != plant.is_active:
                target_state = (
                    MasterLifecycleState.ACTIVE
                    if changes["is_active"]
                    else MasterLifecycleState.INACTIVE
                )
                PLANT_LIFECYCLE.assert_transition(current_state, target_state)

            for field, value in changes.items():
                setattr(plant, field, value)
            self.repo.touch(plant)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_PLANT,
                entity_id=plant.id,
                entity_code=plant.code,
                summary=f"Updated plant {plant.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(plant, _PLANT_AUDITED_FIELDS),
            )
            if target_state is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ENTITY_PLANT,
                    entity_id=plant.id,
                    entity_code=plant.code,
                    from_status=str(current_state),
                    to_status=str(target_state),
                    user_id=self.tenant.user_id,
                    reason="Activation flag changed through plant update",
                )

        return PlantRead.model_validate(plant)

    async def delete_plant(self, plant_id: uuid.UUID, *, reason: str) -> None:
        async with transaction(self.session):
            plant = await self.repo.get_or_404(plant_id)
            current = lifecycle_state(plant)
            PLANT_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if await self.repo.referenced_by_users(plant.id):
                raise referenced_in_use(
                    f"Plant {plant.code} is still assigned to one or more users.",
                    plant_code=plant.code,
                )
            if await self.repo.referenced_by_departments(plant.id):
                raise referenced_in_use(
                    f"Plant {plant.code} still has departments. Reassign those first.",
                    plant_code=plant.code,
                )
            if await self.repo.referenced_by_warehouses(plant.id):
                raise referenced_in_use(
                    f"Plant {plant.code} still has warehouses. Reassign those first.",
                    plant_code=plant.code,
                )
            if await self.repo.referenced_by_cost_centres(plant.id):
                raise referenced_in_use(
                    f"Plant {plant.code} still has cost centres. Reassign or retire those first.",
                    plant_code=plant.code,
                )
            carriers = await self.repo.referenced_by_carriers(plant.id)
            if carriers:
                raise referenced_in_use(
                    f"Plant {plant.code} is still referenced by {', '.join(carriers)}. "
                    "Reassign or retire those first, or deactivate the plant instead.",
                    plant_code=plant.code,
                    referenced_by=carriers,
                )

            plant.is_active = False
            await self.repo.soft_delete(plant)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_PLANT,
                entity_id=plant.id,
                entity_code=plant.code,
                summary=f"Retired plant {plant.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    async def _assert_branch(self, branch_id: uuid.UUID | None) -> None:
        """Fail with a readable error rather than a foreign-key violation.

        The database would reject a dangling reference anyway, but as an opaque
        IntegrityError. Checking here also catches the case the FK cannot see: an
        id that exists in *another* tenant.
        """
        if branch_id is not None and not await self.repo.branch_exists(branch_id):
            raise NotFoundError("Branch", branch_id)


# -----------------------------------------------------------------------------
# Department
# -----------------------------------------------------------------------------


class DepartmentService:
    """Department master-data operations, including the tree guard."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = DepartmentRepository(session, tenant)

    async def list_departments(
        self, filters: DepartmentFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[DepartmentRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [DepartmentRead.model_validate(row) for row in rows], pagination

    async def get_department(self, department_id: uuid.UUID) -> DepartmentRead:
        return DepartmentRead.model_validate(await self.repo.get_or_404(department_id))

    async def create_department(self, payload: DepartmentCreate) -> DepartmentRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Department code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            await self._assert_references(payload.plant_id, payload.head_user_id)
            if payload.parent_id is not None:
                await self._assert_parent(node_id=None, parent_id=payload.parent_id)
            if payload.cost_centre_id is not None:
                await require_cost_centre(self.session, self.tenant, payload.cost_centre_id)

            department = Department(**payload.model_dump())
            self.repo.add(department)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_DEPARTMENT,
                entity_id=department.id,
                entity_code=department.code,
                summary=f"Created department {department.code} ({department.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(department, _DEPARTMENT_AUDITED_FIELDS),
            )

        return DepartmentRead.model_validate(department)

    async def update_department(
        self, department_id: uuid.UUID, payload: DepartmentUpdate
    ) -> DepartmentRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            department = await self.repo.get_or_404(department_id)
            before = snapshot(department, _DEPARTMENT_AUDITED_FIELDS)
            current_state = lifecycle_state(department)

            # Only a *changed* plant or head is checked. The edit form sends back
            # the head it found, and a head retired since otherwise answered
            # "User not found" to every later edit - including the one that
            # clears this department's default cost centre, which the cost-centre
            # deactivation guard asks for.
            new_plant = changes.get("plant_id")
            new_head = changes.get("head_user_id")
            await self._assert_references(
                new_plant if new_plant != department.plant_id else None,
                new_head if new_head != department.head_user_id else None,
            )

            new_parent = changes.get("parent_id")
            if new_parent is not None and new_parent != department.parent_id:
                await self._assert_parent(node_id=department.id, parent_id=new_parent)

            # Only a *new* default is validated: re-sending the one already held
            # is not a fresh assignment, and the cost-centre deactivation guard
            # keeps a department's default active anyway.
            new_centre = changes.get("cost_centre_id")
            if new_centre is not None and new_centre != department.cost_centre_id:
                await require_cost_centre(self.session, self.tenant, new_centre)

            target_state: MasterLifecycleState | None = None
            if "is_active" in changes and changes["is_active"] != department.is_active:
                target_state = (
                    MasterLifecycleState.ACTIVE
                    if changes["is_active"]
                    else MasterLifecycleState.INACTIVE
                )
                DEPARTMENT_LIFECYCLE.assert_transition(current_state, target_state)

            for field, value in changes.items():
                setattr(department, field, value)
            self.repo.touch(department)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_DEPARTMENT,
                entity_id=department.id,
                entity_code=department.code,
                summary=f"Updated department {department.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(department, _DEPARTMENT_AUDITED_FIELDS),
            )
            if target_state is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ENTITY_DEPARTMENT,
                    entity_id=department.id,
                    entity_code=department.code,
                    from_status=str(current_state),
                    to_status=str(target_state),
                    user_id=self.tenant.user_id,
                    reason="Activation flag changed through department update",
                )

        return DepartmentRead.model_validate(department)

    async def delete_department(self, department_id: uuid.UUID, *, reason: str) -> None:
        async with transaction(self.session):
            department = await self.repo.get_or_404(department_id)
            current = lifecycle_state(department)
            DEPARTMENT_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if await self.repo.has_live_children(department.id):
                raise referenced_in_use(
                    f"Department {department.code} still has sub-departments. "
                    "Retire or reparent those first.",
                    department_code=department.code,
                )
            if await self.repo.referenced_by_users(department.id):
                raise referenced_in_use(
                    f"Department {department.code} is still assigned to one or more users.",
                    department_code=department.code,
                )
            carriers = await self.repo.referenced_by_carriers(department.id)
            if carriers:
                raise referenced_in_use(
                    f"Department {department.code} is still referenced by {', '.join(carriers)}. "
                    "Reassign or retire those first, or deactivate the department instead.",
                    department_code=department.code,
                    referenced_by=carriers,
                )

            department.is_active = False
            await self.repo.soft_delete(department)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_DEPARTMENT,
                entity_id=department.id,
                entity_code=department.code,
                summary=f"Retired department {department.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    async def _assert_parent(self, *, node_id: uuid.UUID | None, parent_id: uuid.UUID) -> None:
        """Load the proposed parent, then apply the pure tree rules."""
        parent = await self.repo.get(parent_id)
        if parent is None:
            raise NotFoundError("Parent department", parent_id)

        ancestors = await self.repo.ancestor_ids(parent.id)
        assert_parent_allowed(
            entity="department",
            node_id=node_id,
            parent_id=parent_id,
            parent_ancestor_ids=ancestors,
        )

    async def _assert_references(
        self, plant_id: uuid.UUID | None, head_user_id: uuid.UUID | None
    ) -> None:
        if plant_id is not None and not await self.repo.plant_exists(plant_id):
            raise NotFoundError("Plant", plant_id)
        if head_user_id is not None and not await self.repo.user_exists(head_user_id):
            raise NotFoundError("User", head_user_id)


# -----------------------------------------------------------------------------
# Cost centre
# -----------------------------------------------------------------------------


class CostCentreService:
    """Cost-centre master data, including the tree guard and the retire rules.

    Two retirement levels, deliberately different:

    * **Deactivating** stops a centre being offered for new allocations. It is
      refused only while something would go on *handing it out*: an active
      child (a live branch under a dead one), a department or a work centre
      naming it as a default. Documents keep their reference.
    * **Archiving** removes it from every list. It is refused while anything at
      all still names it - a child, a department, a work centre or any document
      - except posted ledger history, which keeps its centre for good.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = CostCentreRepository(session, tenant)

    async def list_cost_centres(
        self, filters: CostCentreFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[CostCentreRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [CostCentreRead.model_validate(row) for row in rows], pagination

    async def get_cost_centre(self, cost_centre_id: uuid.UUID) -> CostCentreRead:
        return CostCentreRead.model_validate(await self.repo.get_or_404(cost_centre_id))

    async def create_cost_centre(self, payload: CostCentreCreate) -> CostCentreRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Cost centre code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )
            await self._assert_references(payload.plant_id, payload.head_user_id)
            if payload.parent_id is not None:
                parent = await self._assert_parent(
                    node_id=None, parent_id=payload.parent_id, subtree_height=0
                )
                if payload.is_active:
                    _assert_parent_active(parent)

            centre = CostCentre(**payload.model_dump())
            self.repo.add(centre)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_COST_CENTRE,
                entity_id=centre.id,
                entity_code=centre.code,
                summary=f"Created cost centre {centre.code} ({centre.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(centre, _COST_CENTRE_AUDITED_FIELDS),
            )

        return CostCentreRead.model_validate(centre)

    async def update_cost_centre(
        self, cost_centre_id: uuid.UUID, payload: CostCentreUpdate
    ) -> CostCentreRead:
        changes = {
            field: value
            for field, value in payload.model_dump(exclude_unset=True).items()
            if not (value is None and field in _COST_CENTRE_REQUIRED_FIELDS)
        }
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            centre = await self.repo.get_or_404(cost_centre_id)
            before = snapshot(centre, _COST_CENTRE_AUDITED_FIELDS)
            current_state = lifecycle_state(centre)

            # Only a *changed* plant or head is checked, the rule every carrier
            # applies to a cost centre. The edit form sends back the plant and
            # head it found; re-checking them made a centre whose head had
            # since been retired answer "User not found" to every later edit,
            # and an editor shown no head picker had no way to clear it.
            new_plant = changes.get("plant_id")
            new_head = changes.get("head_user_id")
            await self._assert_references(
                new_plant if new_plant != centre.plant_id else None,
                new_head if new_head != centre.head_user_id else None,
            )

            parent_changing = "parent_id" in changes and changes["parent_id"] != centre.parent_id
            parent_id = changes.get("parent_id", centre.parent_id)
            parent: CostCentre | None = None
            if parent_changing and parent_id is not None:
                parent = await self._assert_parent(
                    node_id=centre.id,
                    parent_id=parent_id,
                    subtree_height=await self.repo.subtree_height(centre.id),
                )

            target_state: MasterLifecycleState | None = None
            if "is_active" in changes and changes["is_active"] != centre.is_active:
                target_state = (
                    MasterLifecycleState.ACTIVE
                    if changes["is_active"]
                    else MasterLifecycleState.INACTIVE
                )
                COST_CENTRE_LIFECYCLE.assert_transition(current_state, target_state)
                if target_state is MasterLifecycleState.INACTIVE:
                    await self._assert_may_deactivate(centre)

            # An active centre may not hang under an inactive one, whichever
            # edit would produce that: activating the child or moving it.
            will_be_active = changes.get("is_active", centre.is_active)
            if (
                will_be_active
                and parent_id is not None
                and (parent_changing or target_state is not None)
            ):
                if parent is None:
                    parent = await self.repo.get(parent_id)
                if parent is not None:
                    _assert_parent_active(parent)

            for field, value in changes.items():
                setattr(centre, field, value)
            self.repo.touch(centre)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_COST_CENTRE,
                entity_id=centre.id,
                entity_code=centre.code,
                summary=f"Updated cost centre {centre.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(centre, _COST_CENTRE_AUDITED_FIELDS),
            )
            if target_state is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ENTITY_COST_CENTRE,
                    entity_id=centre.id,
                    entity_code=centre.code,
                    from_status=str(current_state),
                    to_status=str(target_state),
                    user_id=self.tenant.user_id,
                    reason="Activation flag changed through cost centre update",
                )

        return CostCentreRead.model_validate(centre)

    async def delete_cost_centre(self, cost_centre_id: uuid.UUID, *, reason: str) -> None:
        """Retire (soft-delete) a cost centre nothing but posted history names."""
        async with transaction(self.session):
            centre = await self.repo.get_or_404(cost_centre_id)
            current = lifecycle_state(centre)
            COST_CENTRE_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if await self.repo.has_live_children(centre.id):
                raise referenced_in_use(
                    f"Cost centre {centre.code} still has sub-centres. Retire or move those first.",
                    cost_centre_code=centre.code,
                )
            if await self.repo.referenced_by_departments(centre.id):
                raise referenced_in_use(
                    f"Cost centre {centre.code} is still the default of one or more "
                    "departments. Clear it there first.",
                    cost_centre_code=centre.code,
                )
            carriers = await self.repo.referenced_by_carriers(centre.id)
            if carriers:
                # ASSUMPTION: any row that names the centre blocks archiving,
                # whatever that document's status - see docs/assumptions.md
                # §B1-3. Deactivation is the way to stop using a centre that
                # documents still carry.
                raise referenced_in_use(
                    f"Cost centre {centre.code} is still referenced by {', '.join(carriers)}. "
                    "Deactivate it instead so it stops being offered for new documents.",
                    cost_centre_code=centre.code,
                    referenced_by=carriers,
                )

            centre.is_active = False
            await self.repo.soft_delete(centre)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_COST_CENTRE,
                entity_id=centre.id,
                entity_code=centre.code,
                summary=f"Retired cost centre {centre.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    async def _assert_may_deactivate(self, centre: CostCentre) -> None:
        """Refuse to switch off a centre something would keep handing out."""
        # ASSUMPTION: deactivation is refused only by things that hand the
        # centre out - an active child, a department default, a work centre.
        # Documents keep their reference. docs/assumptions.md §B1-3.
        if await self.repo.has_active_children(centre.id):
            raise referenced_in_use(
                f"Cost centre {centre.code} still has active sub-centres. "
                "Deactivate or move those first.",
                cost_centre_code=centre.code,
            )
        if await self.repo.referenced_by_departments(centre.id):
            raise referenced_in_use(
                f"Cost centre {centre.code} is the default of one or more departments. "
                "Clear it there first.",
                cost_centre_code=centre.code,
            )
        defaults = await self.repo.referenced_as_default(centre.id)
        if defaults:
            raise referenced_in_use(
                f"Cost centre {centre.code} is still assigned to {', '.join(defaults)}. "
                "Clear it there first.",
                cost_centre_code=centre.code,
                referenced_by=defaults,
            )

    async def _assert_parent(
        self, *, node_id: uuid.UUID | None, parent_id: uuid.UUID, subtree_height: int
    ) -> CostCentre:
        """Load the proposed parent, then apply the pure tree rules."""
        parent = await self.repo.get(parent_id)
        if parent is None:
            raise NotFoundError("Parent cost centre", parent_id)

        ancestors = await self.repo.ancestor_ids(parent.id)
        assert_parent_allowed(
            entity="cost centre",
            node_id=node_id,
            parent_id=parent_id,
            parent_ancestor_ids=ancestors,
            max_depth=MAX_COST_CENTRE_DEPTH,
            subtree_height=subtree_height,
        )
        return parent

    async def _assert_references(
        self, plant_id: uuid.UUID | None, head_user_id: uuid.UUID | None
    ) -> None:
        if plant_id is not None and not await self.repo.plant_exists(plant_id):
            raise NotFoundError("Plant", plant_id)
        if head_user_id is not None and not await self.repo.user_exists(head_user_id):
            raise NotFoundError("User", head_user_id)


def _assert_parent_active(parent: CostCentre) -> None:
    """An active centre under an inactive one would be offered while its parent is not."""
    # ASSUMPTION: active only under active, whichever edit would produce the
    # opposite. docs/assumptions.md §B1-5.
    if not parent.is_active:
        raise BusinessRuleError(
            f"Parent cost centre {parent.code} is inactive; an active cost centre cannot sit "
            "under it. Activate the parent first, or create this one inactive.",
            details={"parent_id": str(parent.id), "parent_code": parent.code},
        )


# -----------------------------------------------------------------------------
# Public seam - how every other module validates a cost centre it was handed
# -----------------------------------------------------------------------------
# Organisation is the lowest module in the dependency order, so routing,
# production, job work, inventory and finance all call these rather than
# reading ``cost_centres`` themselves. A foreign key to a UUID is satisfied by
# another company's row; these read through the tenant-scoped repository, so a
# foreign id answers exactly as a missing one does - 404 - and never confirms it
# exists elsewhere.

#: The verb phrase an inactive centre's refusal ends with.
_NOT_ASSIGNABLE = "cannot be assigned"


def _not_assignable(code: str, cost_centre_id: uuid.UUID, field: str) -> ValidationError:
    message = inactive_record(COST_CENTRE_ENTITY, code, _NOT_ASSIGNABLE)
    return ValidationError(
        message,
        details={"fields": {field: [message]}, "cost_centre_id": str(cost_centre_id)},
    )


async def require_cost_centre(
    session: AsyncSession,
    tenant: TenantContext,
    cost_centre_id: uuid.UUID,
    *,
    field: str = "cost_centre_id",
) -> CostCentre:
    """The cost centre a write is about to name, if it may name it.

    Live and in this tenant, or :class:`NotFoundError` - another company's id
    included. Active, or a 422 carrying ``details.fields[field]`` so a form can
    show the refusal against the control that sent it. Only a *new* assignment
    should come through here: a document re-saving the centre it already holds
    is not allocating anything, and keeps its reference after deactivation.
    """
    centre = await CostCentreRepository(session, tenant).get(cost_centre_id)
    if centre is None:
        raise NotFoundError(COST_CENTRE_ENTITY, cost_centre_id)
    if not centre.is_active:
        raise _not_assignable(centre.code, centre.id, field)
    return centre


async def require_cost_centres(
    session: AsyncSession,
    tenant: TenantContext,
    references: Sequence[tuple[str, uuid.UUID]],
) -> dict[uuid.UUID, CostCentreFacts]:
    """The batch form, for a document naming several: ``(field, id)`` pairs.

    One statement for the whole set, never one per line. The first unknown id
    raises :class:`NotFoundError`; every inactive one is reported at once,
    each against its own field (``lines.2.cost_centre_id``).
    """
    if not references:
        return {}
    facts = await CostCentreRepository(session, tenant).facts(
        (cost_centre_id for _, cost_centre_id in references), include_archived=False
    )
    for _, cost_centre_id in references:
        if cost_centre_id not in facts:
            raise NotFoundError(COST_CENTRE_ENTITY, cost_centre_id)

    refused: dict[str, list[str]] = {}
    for field, cost_centre_id in references:
        found = facts[cost_centre_id]
        if not found.is_assignable:
            refused[field] = [inactive_record(COST_CENTRE_ENTITY, found.code, _NOT_ASSIGNABLE)]
    if refused:
        first = next(iter(refused.values()))[0]
        raise ValidationError(first, details={"fields": refused})
    return facts


async def active_cost_centre_ids(
    session: AsyncSession, tenant: TenantContext, cost_centre_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of these ids name a live, active cost centre of this tenant."""
    facts = await CostCentreRepository(session, tenant).facts(
        cost_centre_ids, include_archived=False
    )
    return {cost_centre_id for cost_centre_id, found in facts.items() if found.is_assignable}


async def cost_centre_facts(
    session: AsyncSession,
    tenant: TenantContext,
    cost_centre_ids: Iterable[uuid.UUID],
    *,
    include_archived: bool = False,
) -> dict[uuid.UUID, CostCentreFacts]:
    """Tenant-scoped facts for these ids, in one statement.

    ``include_archived`` is for history: a ledger reversal copies the centre of
    the leg it undoes and must find it even after it was retired, because
    retiring a centre must not make the journals posted to it irreversible.
    """
    return await CostCentreRepository(session, tenant).facts(
        cost_centre_ids, include_archived=include_archived
    )


# -----------------------------------------------------------------------------
# Financial year
# -----------------------------------------------------------------------------


class FinancialYearService:
    """Accounting-period operations: open, adjust, close, reopen."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = FinancialYearRepository(session, tenant)

    async def list_years(
        self, filters: FinancialYearFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[FinancialYearRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [FinancialYearRead.model_validate(row) for row in rows], pagination

    async def get_year(self, year_id: uuid.UUID) -> FinancialYearRead:
        return FinancialYearRead.model_validate(await self.repo.get_or_404(year_id))

    async def create_year(self, payload: FinancialYearCreate) -> FinancialYearRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Financial year '{payload.code}' already exists in this company.",
                    details={"code": payload.code},
                )

            assert_period_valid(payload.start_date, payload.end_date)
            if payload.is_active:
                await self._assert_no_active_overlap(
                    payload.start_date, payload.end_date, exclude_id=None
                )

            year = FinancialYear(**payload.model_dump())
            self.repo.add(year)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                entity_id=year.id,
                entity_code=year.code,
                summary=(
                    f"Opened financial year {year.code} "
                    f"({year.start_date.isoformat()} to {year.end_date.isoformat()})"
                ),
                user_id=self.tenant.user_id,
                new_values=snapshot(year, _FINANCIAL_YEAR_AUDITED_FIELDS),
            )

        return FinancialYearRead.model_validate(year)

    async def update_year(
        self, year_id: uuid.UUID, payload: FinancialYearUpdate
    ) -> FinancialYearRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            year = await self.repo.get_or_404(year_id)
            if year.is_closed:
                raise BusinessRuleError(
                    f"Financial year {year.code} is closed and can no longer be edited.",
                    details={"code": year.code},
                )

            before = snapshot(year, _FINANCIAL_YEAR_AUDITED_FIELDS)
            current_state = lifecycle_state(year)

            start_date = changes.get("start_date", year.start_date)
            end_date = changes.get("end_date", year.end_date)
            will_be_active = changes.get("is_active", year.is_active)

            assert_period_valid(start_date, end_date)
            if will_be_active:
                await self._assert_no_active_overlap(start_date, end_date, exclude_id=year.id)

            target_state: MasterLifecycleState | None = None
            if "is_active" in changes and changes["is_active"] != year.is_active:
                target_state = (
                    MasterLifecycleState.ACTIVE
                    if changes["is_active"]
                    else MasterLifecycleState.INACTIVE
                )
                FINANCIAL_YEAR_LIFECYCLE.assert_transition(current_state, target_state)

            for field, value in changes.items():
                setattr(year, field, value)
            self.repo.touch(year)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                entity_id=year.id,
                entity_code=year.code,
                summary=f"Updated financial year {year.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(year, _FINANCIAL_YEAR_AUDITED_FIELDS),
            )
            if target_state is not None:
                await record_status_change(
                    self.session,
                    company_id=self.tenant.company_id,
                    entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                    entity_id=year.id,
                    entity_code=year.code,
                    from_status=str(current_state),
                    to_status=str(target_state),
                    user_id=self.tenant.user_id,
                    reason="Activation flag changed through financial year update",
                )

        return FinancialYearRead.model_validate(year)

    async def close_year(self, year_id: uuid.UUID, *, reason: str) -> FinancialYearRead:
        """Close a period so nothing more can be posted into it.

        One-way: only a platform administrator can reopen it, which is what makes
        a filed period trustworthy. Closing also clears ``is_active`` - a closed
        year is history, and leaving it active would keep it occupying its date
        range against the overlap rule.
        """
        async with transaction(self.session):
            year = await self.repo.get_or_404(year_id)
            current = financial_year_state(year)
            FINANCIAL_YEAR_MACHINE.assert_transition(current, FinancialYearState.CLOSED)

            year.is_closed = True
            year.is_active = False
            self.repo.touch(year)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                entity_id=year.id,
                entity_code=year.code,
                from_status=str(current),
                to_status=str(FinancialYearState.CLOSED),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return FinancialYearRead.model_validate(year)

    async def reopen_year(self, year_id: uuid.UUID, *, reason: str) -> FinancialYearRead:
        """Undo a close. Platform administrators only.

        The authority check lives here as well as on the route: a service method
        that can silently reopen a filed period must not depend on every future
        caller remembering to gate itself.
        """
        if not self.tenant.is_superuser:
            raise AuthorizationError(
                "Reopening a closed financial year is restricted to platform administrators."
            )

        async with transaction(self.session):
            year = await self.repo.get_or_404(year_id)
            current = financial_year_state(year)
            FINANCIAL_YEAR_MACHINE.assert_transition(current, FinancialYearState.OPEN)

            # Reopening makes the year active again, so its range competes with
            # the other open periods once more and has to be re-checked.
            await self._assert_no_active_overlap(year.start_date, year.end_date, exclude_id=year.id)

            year.is_closed = False
            year.is_active = True
            self.repo.touch(year)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                entity_id=year.id,
                entity_code=year.code,
                from_status=str(current),
                to_status=str(FinancialYearState.OPEN),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return FinancialYearRead.model_validate(year)

    async def delete_year(self, year_id: uuid.UUID, *, reason: str) -> None:
        """Retire a financial year that was created in error.

        Refused once the year is closed, and refused once any document has been
        numbered inside its period - the numbering series must stay contiguous
        and traceable to a period that still exists.
        """
        async with transaction(self.session):
            year = await self.repo.get_or_404(year_id)
            current = lifecycle_state(year)
            FINANCIAL_YEAR_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if year.is_closed:
                raise referenced_in_use(
                    f"Financial year {year.code} is closed and is part of the filed record.",
                    financial_year_code=year.code,
                )
            if await self.repo.document_numbers_issued(year.code):
                raise referenced_in_use(
                    f"Documents have already been numbered in financial year {year.code}.",
                    financial_year_code=year.code,
                )

            year.is_active = False
            await self.repo.soft_delete(year)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_FINANCIAL_YEAR,
                entity_id=year.id,
                entity_code=year.code,
                summary=f"Retired financial year {year.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    async def _assert_no_active_overlap(
        self, start_date: date, end_date: date, *, exclude_id: uuid.UUID | None
    ) -> None:
        """Refuse a period that intersects another active one.

        Runs inside the caller's transaction, so it cannot be separated from the
        write it guards. It is still a check-then-write and therefore not proof
        against two simultaneous creates; see
        :meth:`FinancialYearRepository.overlapping_active` for the ``btree_gist``
        EXCLUDE constraint that would make the guarantee absolute.
        """
        clash = await self.repo.overlapping_active(start_date, end_date, exclude_id=exclude_id)
        if clash is not None:
            raise BusinessRuleError(
                f"This period overlaps active financial year {clash.code} "
                f"({clash.start_date.isoformat()} to {clash.end_date.isoformat()}).",
                details={
                    "conflicting_financial_year_id": str(clash.id),
                    "conflicting_code": clash.code,
                },
            )


def default_financial_year_code(on: date | None = None, start_month: int = 4) -> str:
    """The conventional label for the year containing ``on`` (``2026-27``).

    Exposed so the UI can pre-fill the code with the same string
    ``src.common.codes`` will use as the document-numbering period key. Keeping
    the two aligned is what lets
    :meth:`FinancialYearRepository.document_numbers_issued` connect a year to
    the documents numbered inside it.
    """
    return financial_year_key(on, start_month)


__all__ = [
    "AUDIT_ENTITY_BRANCH",
    "AUDIT_ENTITY_COMPANY",
    "AUDIT_ENTITY_COST_CENTRE",
    "AUDIT_ENTITY_DEPARTMENT",
    "AUDIT_ENTITY_FINANCIAL_YEAR",
    "AUDIT_ENTITY_PLANT",
    "BRANCH_LIFECYCLE",
    "COST_CENTRE_ENTITY",
    "COST_CENTRE_LIFECYCLE",
    "DEPARTMENT_LIFECYCLE",
    "FINANCIAL_YEAR_LIFECYCLE",
    "FINANCIAL_YEAR_MACHINE",
    "MAX_COST_CENTRE_DEPTH",
    "MAX_DEPARTMENT_DEPTH",
    "MAX_FINANCIAL_YEAR_DAYS",
    "PLANT_LIFECYCLE",
    "BranchService",
    "CompanyService",
    "CostCentreFacts",
    "CostCentreService",
    "DepartmentService",
    "FinancialYearService",
    "FinancialYearState",
    "MasterLifecycleState",
    "PlantService",
    "active_cost_centre_ids",
    "assert_parent_allowed",
    "assert_period_valid",
    "cost_centre_facts",
    "default_financial_year_code",
    "financial_year_state",
    "lifecycle_state",
    "master_lifecycle",
    "ranges_overlap",
    "referenced_in_use",
    "require_cost_centre",
    "require_cost_centres",
]
