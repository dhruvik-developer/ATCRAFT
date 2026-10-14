"""Business rules for warehouse and storage-location master data.

Rules enforced here, and the reason each one exists:

* **A code is never reused.** It is quoted on labels and historical documents,
  so a second warehouse answering to ``RM-01`` makes the archive ambiguous.
* **The hierarchy stays a tree.** A cycle in ``parent_warehouse_id`` turns every
  roll-up report into an infinite loop, so a parent that is already a descendant
  is refused before it is written.
* **At most one default warehouse per company**, cleared in the same transaction
  that sets the new one and backed by a partial unique index.
* **Retirement is soft and conditional.** A warehouse still holding stock, still
  parenting live sub-stores, or still flagged as the company default cannot be
  retired - the stock would become unreachable while remaining on the balance
  sheet.
* **Every state change goes through a state machine** and is written to the
  audit log in the same transaction as the change itself.

The stock check reads ``inventory_balances`` through the repository's raw-SQL
probe rather than importing the inventory module: master data sits *below* the
inventory engine, and inverting that dependency would make either module
impossible to build or test on its own.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
)
from src.core.logging import get_logger
from src.core.messages import NO_FIELDS_TO_UPDATE
from src.core.pagination import PageParams, SortParams
from src.core.permissions import Permission
from src.core.responses import OffsetPagination
from src.core.state_machine import StateMachine, Transition
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit, record_status_change, snapshot
from src.modules.warehouses.models import StorageLocation, Warehouse, WarehouseType
from src.modules.warehouses.repository import (
    StorageLocationRepository,
    WarehouseRepository,
)
from src.modules.warehouses.schemas import (
    StorageLocationCreate,
    StorageLocationFilters,
    StorageLocationRead,
    StorageLocationUpdate,
    WarehouseCreate,
    WarehouseFilters,
    WarehouseOption,
    WarehouseRead,
    WarehouseUpdate,
)

log = get_logger(__name__)

AUDIT_ENTITY_WAREHOUSE = "warehouse"
AUDIT_ENTITY_LOCATION = "storage_location"

# ASSUMPTION: a warehouse (or bin) hierarchy deeper than five levels is a data
# entry mistake, not a real layout - plant > store > sub-store > rack-group is
# already four. The limit is a named constant so it can be raised deliberately
# if a site genuinely needs more; it is not a database constraint because
# loosening it must not require a migration.
MAX_HIERARCHY_DEPTH = 5

#: Fields captured before and after an edit so the audit entry records exactly
#: what changed. Deliberately explicit rather than "every column": created_at and
#: the actor columns are not business changes.
_WAREHOUSE_AUDITED_FIELDS = [
    "code",
    "name",
    "warehouse_type",
    "branch_id",
    "plant_id",
    "parent_warehouse_id",
    "manager_user_id",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "state_code",
    "postal_code",
    "country",
    "phone",
    "email",
    "is_active",
    "is_default",
    "allow_negative_stock",
    "notes",
]

_LOCATION_AUDITED_FIELDS = [
    "code",
    "name",
    "location_type",
    "warehouse_id",
    "parent_location_id",
    "is_active",
    "capacity_quantity",
    "capacity_uom",
    "max_weight_kg",
    "max_volume_m3",
    "barcode",
    "notes",
]


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------


class MasterLifecycleState(StrEnum):
    """Lifecycle of a master record.

    There is no ``status`` column: the state is derived from ``is_active`` and
    ``deleted_at``, which are the two flags the rest of the system already reads.
    Storing a third, redundant column would let it disagree with them. Routing
    the transitions through a :class:`StateMachine` still gives one place where
    the legal moves are declared, tested and rendered for the UI (section 18).
    """

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    ARCHIVED = "ARCHIVED"


def _lifecycle(entity: str, manage: Permission) -> StateMachine[MasterLifecycleState]:
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
            # Retiring is reversible in principle, but it stops stock movements,
            # so it is marked irreversible for the confirmation prompt.
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
            # A restored record comes back INACTIVE, never ACTIVE: someone must
            # look at it and switch it on before it can accept stock again.
            Transition(
                MasterLifecycleState.ARCHIVED,
                MasterLifecycleState.INACTIVE,
                permission=manage,
                label="Restore",
            ),
        ),
    )


WAREHOUSE_LIFECYCLE = _lifecycle("Warehouse", Permission.WAREHOUSE_MANAGE)
LOCATION_LIFECYCLE = _lifecycle("Storage location", Permission.LOCATION_MANAGE)


def lifecycle_state(record: Warehouse | StorageLocation) -> MasterLifecycleState:
    """Derive the lifecycle state of a master row from its flags."""
    if record.deleted_at is not None:
        return MasterLifecycleState.ARCHIVED
    return MasterLifecycleState.ACTIVE if record.is_active else MasterLifecycleState.INACTIVE


# -----------------------------------------------------------------------------
# Pure rule helpers - unit tested without a database
# -----------------------------------------------------------------------------


def referenced_in_use(message: str, **details: Any) -> BusinessRuleError:
    """The "you cannot remove this, something still points at it" error.

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
    max_depth: int = MAX_HIERARCHY_DEPTH,
) -> None:
    """Reject a parent assignment that would break the tree.

    ``parent_ancestor_ids`` is the chain from the proposed parent up to its root,
    *including the parent itself*, as returned by the repository's recursive
    walk. Three failures are possible:

    * the record is its own parent;
    * the record already appears above the proposed parent, which would close a
      cycle - the case that turns a roll-up query into an infinite loop;
    * the resulting chain would be deeper than :data:`MAX_HIERARCHY_DEPTH`.
    """
    if node_id is not None and parent_id == node_id:
        raise BusinessRuleError(f"A {entity} cannot be its own parent.")

    if node_id is not None and node_id in parent_ancestor_ids:
        raise BusinessRuleError(
            f"That parent is already below this {entity}; the hierarchy must stay a tree.",
            details={"parent_id": str(parent_id)},
        )

    # +1 for the record itself, which sits one level under the proposed parent.
    depth = len(parent_ancestor_ids) + 1
    if depth > max_depth:
        raise BusinessRuleError(
            f"{entity.capitalize()} hierarchy may not be deeper than {max_depth} levels.",
            details={"depth": depth, "max_depth": max_depth},
        )


# -----------------------------------------------------------------------------
# Warehouse service
# -----------------------------------------------------------------------------


class WarehouseService:
    """Warehouse master-data operations. One instance per request."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = WarehouseRepository(session, tenant)
        self.locations = StorageLocationRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_warehouses(
        self, filters: WarehouseFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[WarehouseRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)

        # One extra query for the whole page rather than one per row.
        counts = await self.repo.location_counts([row.id for row in rows])
        items = [self._to_read(row, counts.get(row.id, 0)) for row in rows]
        return items, pagination

    async def get_warehouse(self, warehouse_id: uuid.UUID) -> WarehouseRead:
        warehouse = await self.repo.get_or_404(warehouse_id)
        counts = await self.repo.location_counts([warehouse.id])
        return self._to_read(warehouse, counts.get(warehouse.id, 0))

    async def list_options(self, warehouse_type: WarehouseType | None) -> list[WarehouseOption]:
        rows = await self.repo.list_options(
            warehouse_type=str(warehouse_type) if warehouse_type else None
        )
        return [WarehouseOption.model_validate(row) for row in rows]

    # --- writes -------------------------------------------------------------

    async def create_warehouse(self, payload: WarehouseCreate) -> WarehouseRead:
        async with transaction(self.session):
            if await self.repo.code_taken(payload.code):
                raise DuplicateError(
                    f"Warehouse code '{payload.code}' is already used in this company.",
                    details={"code": payload.code},
                )

            await self._assert_references_exist(
                branch_id=payload.branch_id,
                plant_id=payload.plant_id,
                manager_user_id=payload.manager_user_id,
            )

            if payload.parent_warehouse_id is not None:
                await self._assert_parent(node_id=None, parent_id=payload.parent_warehouse_id)

            # Clearing runs before the insert so the partial unique index never
            # sees two live defaults, even momentarily.
            if payload.is_default:
                await self.repo.clear_default()

            warehouse = Warehouse(**payload.model_dump())
            self.repo.add(warehouse)
            await self.session.flush()

            if warehouse.allow_negative_stock:
                self._log_negative_stock_enabled(warehouse)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_WAREHOUSE,
                entity_id=warehouse.id,
                entity_code=warehouse.code,
                summary=f"Created warehouse {warehouse.code} ({warehouse.name})",
                user_id=self.tenant.user_id,
                new_values=snapshot(warehouse, _WAREHOUSE_AUDITED_FIELDS),
            )

        return self._to_read(warehouse, 0)

    async def update_warehouse(
        self, warehouse_id: uuid.UUID, payload: WarehouseUpdate
    ) -> WarehouseRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            warehouse = await self.repo.get_or_404(warehouse_id)
            before = snapshot(warehouse, _WAREHOUSE_AUDITED_FIELDS)

            await self._assert_references_exist(
                branch_id=changes.get("branch_id"),
                plant_id=changes.get("plant_id"),
                manager_user_id=changes.get("manager_user_id"),
            )

            new_parent = changes.get("parent_warehouse_id")
            if new_parent is not None and new_parent != warehouse.parent_warehouse_id:
                await self._assert_parent(node_id=warehouse.id, parent_id=new_parent)

            for field, value in changes.items():
                setattr(warehouse, field, value)
            self.repo.touch(warehouse)
            await self.session.flush()

            if changes.get("allow_negative_stock") and not before["allow_negative_stock"]:
                self._log_negative_stock_enabled(warehouse)

            after = snapshot(warehouse, _WAREHOUSE_AUDITED_FIELDS)
            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_WAREHOUSE,
                entity_id=warehouse.id,
                entity_code=warehouse.code,
                summary=f"Updated warehouse {warehouse.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=after,
            )

        counts = await self.repo.location_counts([warehouse.id])
        return self._to_read(warehouse, counts.get(warehouse.id, 0))

    async def set_default(self, warehouse_id: uuid.UUID) -> WarehouseRead:
        """Make this the company's default warehouse, demoting the previous one."""
        async with transaction(self.session):
            warehouse = await self.repo.get_or_404(warehouse_id)
            if not warehouse.is_active:
                raise BusinessRuleError(
                    "An inactive warehouse cannot be the company default.",
                    details={"warehouse_code": warehouse.code},
                )

            # Idempotent: re-nominating the current default is a no-op rather
            # than an error, so a retried request does not fail.
            if not warehouse.is_default:
                await self.repo.clear_default(keep_id=warehouse.id)
                warehouse.is_default = True
                self.repo.touch(warehouse)
                await self.session.flush()

                await record_audit(
                    self.session,
                    company_id=self.tenant.company_id,
                    action=AuditAction.CONFIG_CHANGE,
                    entity_type=AUDIT_ENTITY_WAREHOUSE,
                    entity_id=warehouse.id,
                    entity_code=warehouse.code,
                    summary=f"Warehouse {warehouse.code} set as the company default",
                    user_id=self.tenant.user_id,
                    old_values={"is_default": False},
                    new_values={"is_default": True},
                )

        counts = await self.repo.location_counts([warehouse.id])
        return self._to_read(warehouse, counts.get(warehouse.id, 0))

    async def set_active(
        self, warehouse_id: uuid.UUID, *, active: bool, reason: str
    ) -> WarehouseRead:
        """Activate or deactivate, validated by the lifecycle state machine."""
        target = MasterLifecycleState.ACTIVE if active else MasterLifecycleState.INACTIVE

        async with transaction(self.session):
            warehouse = await self.repo.get_or_404(warehouse_id)
            current = lifecycle_state(warehouse)
            WAREHOUSE_LIFECYCLE.assert_transition(current, target)

            if not active and warehouse.is_default:
                raise BusinessRuleError(
                    "The default warehouse cannot be deactivated. Nominate another default first.",
                    details={"warehouse_code": warehouse.code},
                )

            warehouse.is_active = active
            self.repo.touch(warehouse)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_WAREHOUSE,
                entity_id=warehouse.id,
                entity_code=warehouse.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        counts = await self.repo.location_counts([warehouse.id])
        return self._to_read(warehouse, counts.get(warehouse.id, 0))

    async def delete_warehouse(self, warehouse_id: uuid.UUID, *, reason: str) -> None:
        """Retire a warehouse (soft delete), refusing while anything still needs it."""
        async with transaction(self.session):
            warehouse = await self.repo.get_or_404(warehouse_id)
            current = lifecycle_state(warehouse)
            WAREHOUSE_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if warehouse.is_default:
                raise referenced_in_use(
                    "The default warehouse cannot be retired. Nominate another default first.",
                    warehouse_code=warehouse.code,
                )

            if await self.repo.has_live_children(warehouse.id):
                raise referenced_in_use(
                    f"Warehouse {warehouse.code} still has sub-stores. Retire those first.",
                    warehouse_code=warehouse.code,
                )

            # The decisive check: stock that is still on the books must not
            # become unreachable behind a retired warehouse.
            if await self.repo.has_stock_balance(warehouse.id):
                raise referenced_in_use(
                    f"Warehouse {warehouse.code} still holds stock and cannot be retired. "
                    "Transfer or write off the remaining balance first.",
                    warehouse_code=warehouse.code,
                )

            # A bin cannot outlive its building: retiring the warehouse retires
            # its locations in the same transaction - so none of them may still
            # be an item's default bin (docs/assumptions.md §E8-6). Locked
            # first, so an item cannot be pointed at one of them meanwhile.
            await self.locations.lock_for_warehouse_retirement(warehouse.id)
            if await self.locations.has_item_defaults(warehouse_id=warehouse.id):
                raise referenced_in_use(
                    f"Warehouse {warehouse.code} has bins that are items' default bins. "
                    "Give those items other default bins first.",
                    warehouse_code=warehouse.code,
                )
            retired_locations = await self.locations.soft_delete_for_warehouse(warehouse.id)
            warehouse.is_active = False
            await self.repo.soft_delete(warehouse)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_WAREHOUSE,
                entity_id=warehouse.id,
                entity_code=warehouse.code,
                summary=f"Retired warehouse {warehouse.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason, "retired_locations": retired_locations},
            )

    async def restore_warehouse(self, warehouse_id: uuid.UUID) -> WarehouseRead:
        """Bring a retired warehouse back, inactive.

        Its storage locations are *not* restored automatically: they were
        retired as a consequence, and which of them should come back is a
        decision for whoever is reopening the store.
        """
        async with transaction(self.session):
            warehouse = await self.repo.get(warehouse_id, include_deleted=True)
            if warehouse is None:
                raise NotFoundError("Warehouse", warehouse_id)

            current = lifecycle_state(warehouse)
            # Assert the *source* as well, not just the target edge: the machine
            # also allows ACTIVE -> INACTIVE, so checking the edge alone would
            # let "restore" quietly deactivate a warehouse that is still live.
            if current is not MasterLifecycleState.ARCHIVED:
                raise InvalidStateTransitionError(
                    entity="Warehouse",
                    current=str(current),
                    requested="RESTORED",
                    allowed=[str(t) for t in WAREHOUSE_LIFECYCLE.allowed_targets(current)],
                )
            WAREHOUSE_LIFECYCLE.assert_transition(current, MasterLifecycleState.INACTIVE)

            # Another warehouse may have taken the code while this one was
            # retired; restoring it would violate the tenant unique constraint.
            if await self.repo.code_taken(warehouse.code, exclude_id=warehouse.id):
                raise DuplicateError(
                    f"Code '{warehouse.code}' is now used by another warehouse.",
                    details={"code": warehouse.code},
                )

            warehouse.deleted_at = None
            warehouse.is_active = False
            warehouse.is_default = False
            self.repo.touch(warehouse)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_WAREHOUSE,
                entity_id=warehouse.id,
                entity_code=warehouse.code,
                from_status=str(current),
                to_status=str(MasterLifecycleState.INACTIVE),
                user_id=self.tenant.user_id,
                reason="Warehouse restored",
            )

        return self._to_read(warehouse, 0)

    # --- internals ----------------------------------------------------------

    async def _assert_parent(self, *, node_id: uuid.UUID | None, parent_id: uuid.UUID) -> None:
        """Load the proposed parent, then apply the pure tree rules."""
        parent = await self.repo.get(parent_id)
        if parent is None:
            raise NotFoundError("Parent warehouse", parent_id)

        ancestors = await self.repo.ancestor_ids(parent.id)
        assert_parent_allowed(
            entity="warehouse",
            node_id=node_id,
            parent_id=parent_id,
            parent_ancestor_ids=ancestors,
        )

    async def _assert_references_exist(
        self,
        *,
        branch_id: uuid.UUID | None,
        plant_id: uuid.UUID | None,
        manager_user_id: uuid.UUID | None,
    ) -> None:
        """Fail with a readable error rather than a foreign-key violation.

        The database would reject a dangling reference anyway, but as an opaque
        IntegrityError. Checking here also catches the more dangerous case the
        FK cannot see: an id that exists in *another* tenant.
        """
        if branch_id is not None and not await self.repo.branch_exists(branch_id):
            raise NotFoundError("Branch", branch_id)
        if plant_id is not None and not await self.repo.plant_exists(plant_id):
            raise NotFoundError("Plant", plant_id)
        if manager_user_id is not None and not await self.repo.user_exists(manager_user_id):
            raise NotFoundError("User", manager_user_id)

    def _log_negative_stock_enabled(self, warehouse: Warehouse) -> None:
        """Surface the flag in the operational log as well as the audit trail.

        Turning it on weakens the pre-flight stock check for a whole warehouse.
        The audit row records who did it; this line makes it visible to whoever
        is investigating an unexpected negative-looking balance months later.
        """
        log.warning(
            "warehouse.negative_stock_enabled",
            company_id=str(self.tenant.company_id),
            warehouse_id=str(warehouse.id),
            warehouse_code=warehouse.code,
            actor_id=str(self.tenant.user_id),
        )

    @staticmethod
    def _to_read(warehouse: Warehouse, location_count: int) -> WarehouseRead:
        read = WarehouseRead.model_validate(warehouse)
        read.location_count = location_count
        return read


# -----------------------------------------------------------------------------
# Storage location service
# -----------------------------------------------------------------------------


class StorageLocationService:
    """Storage-location operations, always scoped to a warehouse."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant
        self.repo = StorageLocationRepository(session, tenant)
        self.warehouses = WarehouseRepository(session, tenant)

    # --- reads --------------------------------------------------------------

    async def list_locations(
        self, filters: StorageLocationFilters, params: PageParams, sort: SortParams
    ) -> tuple[list[StorageLocationRead], OffsetPagination]:
        stmt = self.repo.build_list_stmt(filters, sort)
        rows, pagination = await self.repo.list_page(params, stmt=stmt, order_by=None)
        return [StorageLocationRead.model_validate(row) for row in rows], pagination

    async def get_location(self, location_id: uuid.UUID) -> StorageLocationRead:
        return StorageLocationRead.model_validate(await self.repo.get_or_404(location_id))

    async def _locked_for_retirement(self, location_id: uuid.UUID) -> StorageLocation:
        location = await self.repo.lock_for_retirement(location_id)
        if location is None:
            raise NotFoundError("Storage location", location_id)
        return location

    # --- writes -------------------------------------------------------------

    async def create_location(
        self, warehouse_id: uuid.UUID, payload: StorageLocationCreate
    ) -> StorageLocationRead:
        async with transaction(self.session):
            warehouse = await self.warehouses.get_or_404(warehouse_id)
            if not warehouse.is_active:
                raise BusinessRuleError(
                    f"Warehouse {warehouse.code} is inactive; it cannot take new locations.",
                    details={"warehouse_code": warehouse.code},
                )

            if await self.repo.code_taken(warehouse_id, payload.code):
                raise DuplicateError(
                    f"Location code '{payload.code}' already exists in warehouse {warehouse.code}.",
                    details={"code": payload.code, "warehouse_code": warehouse.code},
                )

            if payload.parent_location_id is not None:
                await self._assert_parent(
                    node_id=None,
                    parent_id=payload.parent_location_id,
                    warehouse_id=warehouse_id,
                )

            location = StorageLocation(warehouse_id=warehouse_id, **payload.model_dump())
            self.repo.add(location)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.CREATE,
                entity_type=AUDIT_ENTITY_LOCATION,
                entity_id=location.id,
                entity_code=location.code,
                summary=f"Created location {warehouse.code}/{location.code}",
                user_id=self.tenant.user_id,
                new_values=snapshot(location, _LOCATION_AUDITED_FIELDS),
            )

        return StorageLocationRead.model_validate(location)

    async def update_location(
        self, location_id: uuid.UUID, payload: StorageLocationUpdate
    ) -> StorageLocationRead:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise BusinessRuleError(NO_FIELDS_TO_UPDATE)

        async with transaction(self.session):
            location = await self.repo.get_or_404(location_id)
            before = snapshot(location, _LOCATION_AUDITED_FIELDS)

            new_parent = changes.get("parent_location_id")
            if new_parent is not None and new_parent != location.parent_location_id:
                await self._assert_parent(
                    node_id=location.id,
                    parent_id=new_parent,
                    warehouse_id=location.warehouse_id,
                )

            for field, value in changes.items():
                setattr(location, field, value)
            self.repo.touch(location)
            await self.session.flush()

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.UPDATE,
                entity_type=AUDIT_ENTITY_LOCATION,
                entity_id=location.id,
                entity_code=location.code,
                summary=f"Updated location {location.code}",
                user_id=self.tenant.user_id,
                old_values=before,
                new_values=snapshot(location, _LOCATION_AUDITED_FIELDS),
            )

        return StorageLocationRead.model_validate(location)

    async def set_active(
        self, location_id: uuid.UUID, *, active: bool, reason: str
    ) -> StorageLocationRead:
        target = MasterLifecycleState.ACTIVE if active else MasterLifecycleState.INACTIVE

        async with transaction(self.session):
            location = await self._locked_for_retirement(location_id)
            current = lifecycle_state(location)
            LOCATION_LIFECYCLE.assert_transition(current, target)

            # ASSUMPTION: a bin some item names as its default bin cannot be
            # switched off - the cost-centre default rule, for the same reason:
            # every picker would pre-fill a bin the engine then refuses to put
            # stock into. docs/assumptions.md §E8-6.
            if not active and await self.repo.has_item_defaults(location_id=location.id):
                raise referenced_in_use(
                    f"Location {location.code} is the default bin of at least one item. "
                    "Give those items another default bin first.",
                    location_code=location.code,
                )

            location.is_active = active
            self.repo.touch(location)
            await self.session.flush()

            await record_status_change(
                self.session,
                company_id=self.tenant.company_id,
                entity_type=AUDIT_ENTITY_LOCATION,
                entity_id=location.id,
                entity_code=location.code,
                from_status=str(current),
                to_status=str(target),
                user_id=self.tenant.user_id,
                reason=reason,
            )

        return StorageLocationRead.model_validate(location)

    async def delete_location(self, location_id: uuid.UUID, *, reason: str) -> None:
        async with transaction(self.session):
            location = await self._locked_for_retirement(location_id)
            current = lifecycle_state(location)
            LOCATION_LIFECYCLE.assert_transition(current, MasterLifecycleState.ARCHIVED)

            if await self.repo.has_item_defaults(location_id=location.id):
                raise referenced_in_use(
                    f"Location {location.code} is the default bin of at least one item. "
                    "Give those items another default bin first.",
                    location_code=location.code,
                )

            if await self.repo.has_live_children(location.id):
                raise referenced_in_use(
                    f"Location {location.code} still has sub-locations. Retire those first.",
                    location_code=location.code,
                )

            if await self.repo.has_stock_balance(location.id):
                raise referenced_in_use(
                    f"Location {location.code} still holds stock and cannot be retired. "
                    "Move the remaining balance to another location first.",
                    location_code=location.code,
                )

            location.is_active = False
            await self.repo.soft_delete(location)

            await record_audit(
                self.session,
                company_id=self.tenant.company_id,
                action=AuditAction.DELETE,
                entity_type=AUDIT_ENTITY_LOCATION,
                entity_id=location.id,
                entity_code=location.code,
                summary=f"Retired location {location.code}",
                user_id=self.tenant.user_id,
                old_values={"status": str(current)},
                new_values={"status": str(MasterLifecycleState.ARCHIVED)},
                context={"reason": reason},
            )

    # --- internals ----------------------------------------------------------

    async def _assert_parent(
        self, *, node_id: uuid.UUID | None, parent_id: uuid.UUID, warehouse_id: uuid.UUID
    ) -> None:
        parent = await self.repo.get(parent_id)
        if parent is None:
            raise NotFoundError("Parent location", parent_id)
        if parent.warehouse_id != warehouse_id:
            # A bin nested under a bin in another building is not a hierarchy,
            # it is a data-entry accident.
            raise BusinessRuleError(
                "A storage location's parent must be in the same warehouse.",
                details={"parent_id": str(parent_id)},
            )

        ancestors = await self.repo.ancestor_ids(parent.id)
        assert_parent_allowed(
            entity="storage location",
            node_id=node_id,
            parent_id=parent_id,
            parent_ancestor_ids=ancestors,
        )


__all__ = [
    "AUDIT_ENTITY_LOCATION",
    "AUDIT_ENTITY_WAREHOUSE",
    "LOCATION_LIFECYCLE",
    "MAX_HIERARCHY_DEPTH",
    "WAREHOUSE_LIFECYCLE",
    "MasterLifecycleState",
    "StorageLocationService",
    "WarehouseService",
    "assert_parent_allowed",
    "lifecycle_state",
    "referenced_in_use",
]
