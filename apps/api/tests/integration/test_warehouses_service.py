"""Service-level tests for the warehouses module, against a real PostgreSQL.

These cover the behaviour only a database can prove: the recursive ancestor walk
that keeps the hierarchy a tree, the single-default rule, tenant isolation, the
cascade from a retired warehouse to its bins, and the raw-SQL stock probe -
both branches of the ``to_regclass`` guard in front of it.

Balance rows are built through the inventory module's own model. This module was
written before inventory existed and used to create a stand-in table instead; a
hand-written stand-in states a row shape its owning module has not defined yet,
so it says nothing about the query the service actually runs and collides with
the real table the moment that module lands.

Fixtures (``session``, ``tenant``, ``company``, ``other_company``, ``user``)
come from ``tests/conftest.py``. The session's outer transaction is rolled back
after every test - PostgreSQL rolls back DDL too, so even the test that hides
``inventory_balances`` leaves it there for the next one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.deps import TenantContext
from src.core.errors import (
    BusinessRuleError,
    DuplicateError,
    ErrorCode,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import InventoryBalance
from src.modules.organization.models import Branch
from src.modules.warehouses.models import StorageLocation, Warehouse
from src.modules.warehouses.schemas import (
    StorageLocationCreate,
    StorageLocationFilters,
    WarehouseCreate,
    WarehouseFilters,
    WarehouseUpdate,
)
from src.modules.warehouses.service import StorageLocationService, WarehouseService
from tests import seed_support

pytestmark = pytest.mark.integration

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _payload(code: str, **overrides: object) -> WarehouseCreate:
    data: dict[str, object] = {
        "code": code,
        "name": f"Store {code}",
        "warehouse_type": "RAW_MATERIAL",
    }
    data.update(overrides)
    return WarehouseCreate(**data)


async def _seed_item(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    """Minimal item (and its unit) so a balance row's foreign keys resolve.

    Built from the products module's own model rather than a hand-written
    ``INSERT``: most of the item master's NOT NULL columns default in Python, so
    a column list here would be long and stale by that module's next release.
    """
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=f"RM-{uuid.uuid4().hex[:8].upper()}",
        name="Stocked part",
    )
    return item.id


async def _post_balance(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    location_id: uuid.UUID | None = None,
    **quantities: object,
) -> None:
    """Write one real ``inventory_balances`` row behind a real item."""
    # The table enforces the contract's check constraints: reserved stock is a
    # subset of what is physically present, so a row naming it without an
    # on-hand quantity is rejected outright. Deriving the on-hand the constraint
    # requires keeps these tests about warehouse retirement rather than about
    # inventory bookkeeping.
    if "on_hand_quantity" not in quantities:
        subsets = ("reserved_quantity", "qc_hold_quantity", "damaged_quantity")
        needed = max(
            (Decimal(str(quantities[column])) for column in subsets if column in quantities),
            default=Decimal("0"),
        )
        if needed > 0:
            quantities = {"on_hand_quantity": needed, **quantities}

    session.add(
        InventoryBalance(
            company_id=company_id,
            item_id=await _seed_item(session, company_id),
            warehouse_id=warehouse_id,
            location_id=location_id,
            **{column: Decimal(str(value)) for column, value in quantities.items()},
        )
    )
    await session.flush()


async def _audit_actions(session: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    """Audit actions for one entity, in the order they were written.

    Ordered by ``id``, not ``created_at``. ``created_at`` defaults to
    PostgreSQL's ``now()``, which is the *transaction* timestamp - every row a
    test writes inside its single transaction carries the identical value, so
    ordering by it is a tie and the row order comes back arbitrary. The primary
    key is UUIDv7 and therefore monotonic, which is the real insertion order.
    """
    rows = await session.execute(
        select(AuditLog.action).where(AuditLog.entity_id == entity_id).order_by(AuditLog.id)
    )
    return [str(action) for action in rows.scalars().all()]


@pytest.fixture
def other_tenant(other_company, user) -> TenantContext:
    """A second tenant, sharing the actor so no extra user row is needed.

    ``created_by_id`` only has to reference a real user; what these tests are
    about is that ``company_id`` scopes every read and write.
    """
    return TenantContext(company_id=other_company.id, user_id=user.id)


# -----------------------------------------------------------------------------
# Creation and editing
# -----------------------------------------------------------------------------


class TestCreateWarehouse:
    async def test_creates_stamps_tenant_and_writes_one_audit_entry(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)

        created = await service.create_warehouse(_payload("rm-01"))

        assert created.code == "RM-01"
        assert created.allow_negative_stock is False
        assert created.location_count == 0

        stored = await session.get(Warehouse, created.id)
        assert stored is not None
        # company_id comes from the tenant context, never from the payload.
        assert stored.company_id == tenant.company_id
        assert stored.created_by_id == tenant.user_id

        assert await _audit_actions(session, created.id) == ["CREATE"]

    async def test_duplicate_code_is_refused_before_the_constraint_fires(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        await service.create_warehouse(_payload("rm-01"))

        with pytest.raises(DuplicateError) as exc:
            await service.create_warehouse(_payload("RM-01", name="Duplicate"))
        assert exc.value.details["code"] == "RM-01"

    async def test_the_same_code_is_free_in_another_company(
        self, session: AsyncSession, tenant: TenantContext, other_tenant: TenantContext
    ) -> None:
        await WarehouseService(session, tenant).create_warehouse(_payload("rm-01"))

        elsewhere = await WarehouseService(session, other_tenant).create_warehouse(
            _payload("rm-01")
        )

        assert elsewhere.code == "RM-01"

    async def test_a_branch_from_another_tenant_is_rejected(
        self, session: AsyncSession, tenant: TenantContext, other_company
    ) -> None:
        """A foreign key would accept this row; tenant isolation must not."""
        branch = Branch(company_id=other_company.id, code="BR1", name="Other branch")
        session.add(branch)
        await session.flush()

        with pytest.raises(NotFoundError):
            await WarehouseService(session, tenant).create_warehouse(
                _payload("rm-01", branch_id=branch.id)
            )

    async def test_update_applies_only_the_supplied_fields(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01", city="Rajkot"))

        updated = await service.update_warehouse(created.id, WarehouseUpdate(name="Renamed"))

        assert updated.name == "Renamed"
        assert updated.city == "Rajkot"
        assert await _audit_actions(session, created.id) == ["CREATE", "UPDATE"]

    async def test_update_records_the_changed_fields_only(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("wip-01", warehouse_type="WIP"))

        await service.update_warehouse(created.id, WarehouseUpdate(allow_negative_stock=True))

        entry = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == created.id, AuditLog.action == "UPDATE"
                )
            )
        ).scalar_one()
        assert entry.changed_fields == ["allow_negative_stock"]
        assert entry.new_values == {"allow_negative_stock": True}
        assert entry.old_values == {"allow_negative_stock": False}

    async def test_an_empty_update_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))

        with pytest.raises(BusinessRuleError, match="No fields"):
            await service.update_warehouse(created.id, WarehouseUpdate())


# -----------------------------------------------------------------------------
# The default warehouse
# -----------------------------------------------------------------------------


class TestDefaultWarehouse:
    async def test_nominating_a_new_default_demotes_the_previous_one(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        first = await service.create_warehouse(_payload("rm-01", is_default=True))
        second = await service.create_warehouse(_payload("fg-01", warehouse_type="FINISHED_GOODS"))

        await service.set_default(second.id)

        defaults = (
            (
                await session.execute(
                    select(Warehouse.code).where(
                        Warehouse.company_id == tenant.company_id,
                        Warehouse.is_default.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert list(defaults) == ["FG-01"]
        assert (await service.get_warehouse(first.id)).is_default is False

    async def test_renominating_the_current_default_is_a_no_op(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01", is_default=True))

        again = await service.set_default(created.id)

        assert again.is_default is True
        # No spurious audit entry for a request that changed nothing.
        assert await _audit_actions(session, created.id) == ["CREATE"]

    async def test_the_default_cannot_be_deactivated(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01", is_default=True))

        with pytest.raises(BusinessRuleError, match="default warehouse cannot be deactivated"):
            await service.set_active(created.id, active=False, reason="Site closed")

    async def test_the_default_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01", is_default=True))

        with pytest.raises(BusinessRuleError) as exc:
            await service.delete_warehouse(created.id, reason="Closing the site")
        assert exc.value.code is ErrorCode.REFERENCED_RECORD_IN_USE


# -----------------------------------------------------------------------------
# Hierarchy, exercised through the real recursive CTE
# -----------------------------------------------------------------------------


class TestHierarchy:
    async def test_a_sub_store_may_hang_off_a_parent(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        parent = await service.create_warehouse(_payload("plant-1"))

        child = await service.create_warehouse(
            _payload("wip-1", warehouse_type="WIP", parent_warehouse_id=parent.id)
        )

        assert child.parent_warehouse_id == parent.id

    async def test_closing_a_cycle_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        root = await service.create_warehouse(_payload("plant-1"))
        middle = await service.create_warehouse(_payload("store-1", parent_warehouse_id=root.id))
        leaf = await service.create_warehouse(
            _payload("wip-1", warehouse_type="WIP", parent_warehouse_id=middle.id)
        )

        # root -> middle -> leaf already exists; pointing root at leaf closes it.
        with pytest.raises(BusinessRuleError, match="stay a tree"):
            await service.update_warehouse(root.id, WarehouseUpdate(parent_warehouse_id=leaf.id))

    async def test_a_warehouse_cannot_become_its_own_parent(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))

        with pytest.raises(BusinessRuleError, match="own parent"):
            await service.update_warehouse(
                created.id, WarehouseUpdate(parent_warehouse_id=created.id)
            )

    async def test_a_parent_in_another_tenant_is_not_found(
        self, session: AsyncSession, tenant: TenantContext, other_tenant: TenantContext
    ) -> None:
        foreign = await WarehouseService(session, other_tenant).create_warehouse(_payload("rm-01"))

        with pytest.raises(NotFoundError):
            await WarehouseService(session, tenant).create_warehouse(
                _payload("rm-02", parent_warehouse_id=foreign.id)
            )


# -----------------------------------------------------------------------------
# Retirement: the stock guard
# -----------------------------------------------------------------------------


class TestRetirement:
    async def test_retires_a_warehouse_that_holds_nothing(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))

        await service.delete_warehouse(created.id, reason="Consolidated into FG-01")

        stored = await session.get(Warehouse, created.id)
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.is_active is False
        assert await _audit_actions(session, created.id) == ["CREATE", "DELETE"]

    async def test_refuses_while_the_warehouse_holds_stock(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await _post_balance(
            session,
            company_id=tenant.company_id,
            warehouse_id=created.id,
            on_hand_quantity="12.5",
        )

        with pytest.raises(BusinessRuleError) as exc:
            await service.delete_warehouse(created.id, reason="Closing the site")
        assert exc.value.code is ErrorCode.REFERENCED_RECORD_IN_USE
        assert "still holds stock" in exc.value.message

        # A refused delete must leave the record exactly as it was.
        assert (await service.get_warehouse(created.id)).deleted_at is None

    async def test_a_zero_balance_row_does_not_block_retirement(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """An emptied warehouse keeps its balance rows; they must not trap it."""
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await _post_balance(session, company_id=tenant.company_id, warehouse_id=created.id)

        await service.delete_warehouse(created.id, reason="Emptied and closed")

        stored = await session.get(Warehouse, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_a_reserved_only_balance_still_blocks_retirement(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Reserved stock is still stock: it is promised to a document."""
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await _post_balance(
            session,
            company_id=tenant.company_id,
            warehouse_id=created.id,
            reserved_quantity="3",
        )

        with pytest.raises(BusinessRuleError):
            await service.delete_warehouse(created.id, reason="Closing the site")

    async def test_another_tenants_balance_does_not_block_retirement(
        self, session: AsyncSession, tenant: TenantContext, other_company
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await _post_balance(
            session,
            company_id=other_company.id,
            warehouse_id=created.id,
            on_hand_quantity="99",
        )

        await service.delete_warehouse(created.id, reason="Emptied and closed")

        stored = await session.get(Warehouse, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_retirement_survives_a_database_without_the_inventory_module(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The other branch of the ``to_regclass`` guard.

        Warehouses ship before inventory does, and on that database the delete
        must not fail on a missing relation. Hiding the table inside the test's
        own transaction is what keeps that branch reachable now that inventory
        has landed - PostgreSQL rolls DDL back with everything else, so the
        table is there again for the next test. Renaming rather than dropping
        also proves the guard reads the catalogue, not an emptied table: the row
        below still exists and would block the delete if it were consulted.
        """
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        if await session.scalar(text("SELECT to_regclass('public.inventory_balances')")):
            await _post_balance(
                session,
                company_id=tenant.company_id,
                warehouse_id=created.id,
                on_hand_quantity="7",
            )
            await session.execute(
                text("ALTER TABLE inventory_balances RENAME TO inventory_balances_absent")
            )

        await service.delete_warehouse(created.id, reason="Closing the site")

        stored = await session.get(Warehouse, created.id)
        assert stored is not None and stored.deleted_at is not None

    async def test_refuses_while_live_sub_stores_remain(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        parent = await service.create_warehouse(_payload("plant-1"))
        await service.create_warehouse(
            _payload("wip-1", warehouse_type="WIP", parent_warehouse_id=parent.id)
        )

        with pytest.raises(BusinessRuleError, match="sub-stores"):
            await service.delete_warehouse(parent.id, reason="Closing the site")

    async def test_retiring_a_warehouse_retires_its_locations(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        locations = StorageLocationService(session, tenant)
        warehouse = await service.create_warehouse(_payload("rm-01"))
        bin_a = await locations.create_location(
            warehouse.id, StorageLocationCreate(code="a-01", name="Bin A1", location_type="BIN")
        )

        await service.delete_warehouse(warehouse.id, reason="Consolidated")

        stored = await session.get(StorageLocation, bin_a.id)
        assert stored is not None
        assert stored.deleted_at is not None
        assert stored.is_active is False

    async def test_restore_brings_it_back_inactive(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await service.delete_warehouse(created.id, reason="Closed for the season")

        restored = await service.restore_warehouse(created.id)

        assert restored.deleted_at is None
        assert restored.is_active is False
        assert restored.is_default is False

    async def test_a_live_warehouse_cannot_be_restored(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Restore must not double as a back-door deactivation."""
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))

        with pytest.raises(InvalidStateTransitionError):
            await service.restore_warehouse(created.id)

        assert (await service.get_warehouse(created.id)).is_active is True


# -----------------------------------------------------------------------------
# Listing and scoping
# -----------------------------------------------------------------------------


class TestListing:
    async def test_search_matches_the_city_as_well_as_the_code(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        await service.create_warehouse(_payload("rm-01", city="Rajkot"))
        await service.create_warehouse(_payload("fg-01", warehouse_type="FINISHED_GOODS"))

        items, pagination = await service.list_warehouses(
            WarehouseFilters(search="rajkot"), PageParams(), SortParams()
        )

        assert [item.code for item in items] == ["RM-01"]
        assert pagination.total == 1

    async def test_retired_warehouses_are_hidden_unless_asked_for(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        created = await service.create_warehouse(_payload("rm-01"))
        await service.delete_warehouse(created.id, reason="Closed")

        visible, _ = await service.list_warehouses(WarehouseFilters(), PageParams(), SortParams())
        with_retired, _ = await service.list_warehouses(
            WarehouseFilters(include_deleted=True), PageParams(), SortParams()
        )

        assert visible == []
        assert [item.code for item in with_retired] == ["RM-01"]

    async def test_location_counts_come_back_with_the_page(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        locations = StorageLocationService(session, tenant)
        first = await service.create_warehouse(_payload("rm-01"))
        await service.create_warehouse(_payload("fg-01", warehouse_type="FINISHED_GOODS"))
        for code in ("a-01", "a-02"):
            await locations.create_location(
                first.id, StorageLocationCreate(code=code, name=code, location_type="BIN")
            )

        items, _ = await service.list_warehouses(WarehouseFilters(), PageParams(), SortParams())

        assert {item.code: item.location_count for item in items} == {"FG-01": 0, "RM-01": 2}

    async def test_sorting_is_restricted_to_the_allow_list(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)

        with pytest.raises(ValidationError):
            await service.list_warehouses(
                WarehouseFilters(), PageParams(), SortParams(sort_by="company_id")
            )

    async def test_pages_do_not_overlap(self, session: AsyncSession, tenant: TenantContext) -> None:
        service = WarehouseService(session, tenant)
        for index in range(5):
            await service.create_warehouse(_payload(f"wh-{index}"))

        first, page_one = await service.list_warehouses(
            WarehouseFilters(), PageParams(page=1, limit=2), SortParams()
        )
        second, _ = await service.list_warehouses(
            WarehouseFilters(), PageParams(page=2, limit=2), SortParams()
        )

        assert [item.code for item in first] == ["WH-0", "WH-1"]
        assert [item.code for item in second] == ["WH-2", "WH-3"]
        assert page_one.total == 5
        assert page_one.has_next is True

    async def test_a_restricted_user_sees_only_permitted_warehouses(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        allowed = await service.create_warehouse(_payload("rm-01"))
        hidden = await service.create_warehouse(_payload("fg-01", warehouse_type="FINISHED_GOODS"))

        restricted = TenantContext(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            allowed_warehouse_ids=(allowed.id,),
        )
        scoped = WarehouseService(session, restricted)

        items, _ = await scoped.list_warehouses(WarehouseFilters(), PageParams(), SortParams())
        assert [item.code for item in items] == ["RM-01"]

        # Out of scope reads as "not found", never as a 403 confirming it exists.
        with pytest.raises(NotFoundError):
            await scoped.get_warehouse(hidden.id)

    async def test_another_tenants_warehouse_is_not_found(
        self, session: AsyncSession, tenant: TenantContext, other_tenant: TenantContext
    ) -> None:
        foreign = await WarehouseService(session, other_tenant).create_warehouse(_payload("rm-01"))

        with pytest.raises(NotFoundError):
            await WarehouseService(session, tenant).get_warehouse(foreign.id)

    async def test_options_are_ordered_with_the_default_first(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        service = WarehouseService(session, tenant)
        await service.create_warehouse(_payload("aa-01"))
        await service.create_warehouse(_payload("zz-01", is_default=True))

        options = await service.list_options(None)

        assert [option.code for option in options] == ["ZZ-01", "AA-01"]


# -----------------------------------------------------------------------------
# Storage locations
# -----------------------------------------------------------------------------


class TestStorageLocations:
    async def test_the_same_bin_code_may_exist_in_two_warehouses(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouses = WarehouseService(session, tenant)
        locations = StorageLocationService(session, tenant)
        first = await warehouses.create_warehouse(_payload("rm-01"))
        second = await warehouses.create_warehouse(
            _payload("fg-01", warehouse_type="FINISHED_GOODS")
        )

        payload = StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN")
        a = await locations.create_location(first.id, payload)
        b = await locations.create_location(second.id, payload)

        assert a.code == b.code == "BIN-01"
        assert a.warehouse_id != b.warehouse_id

    async def test_a_duplicate_bin_code_in_one_warehouse_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouse = await WarehouseService(session, tenant).create_warehouse(_payload("rm-01"))
        locations = StorageLocationService(session, tenant)
        payload = StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN")
        await locations.create_location(warehouse.id, payload)

        with pytest.raises(DuplicateError):
            await locations.create_location(warehouse.id, payload)

    async def test_an_inactive_warehouse_takes_no_new_locations(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouses = WarehouseService(session, tenant)
        warehouse = await warehouses.create_warehouse(_payload("rm-01"))
        await warehouses.set_active(warehouse.id, active=False, reason="Under renovation")

        with pytest.raises(BusinessRuleError, match="inactive"):
            await StorageLocationService(session, tenant).create_location(
                warehouse.id,
                StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN"),
            )

    async def test_a_parent_bin_must_be_in_the_same_warehouse(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouses = WarehouseService(session, tenant)
        locations = StorageLocationService(session, tenant)
        first = await warehouses.create_warehouse(_payload("rm-01"))
        second = await warehouses.create_warehouse(
            _payload("fg-01", warehouse_type="FINISHED_GOODS")
        )
        rack = await locations.create_location(
            first.id, StorageLocationCreate(code="rack-a", name="Rack A", location_type="RACK")
        )

        with pytest.raises(BusinessRuleError, match="same warehouse"):
            await locations.create_location(
                second.id,
                StorageLocationCreate(
                    code="bin-01",
                    name="Bin 1",
                    location_type="BIN",
                    parent_location_id=rack.id,
                ),
            )

    async def test_a_bin_holding_stock_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouse = await WarehouseService(session, tenant).create_warehouse(_payload("rm-01"))
        locations = StorageLocationService(session, tenant)
        bin_a = await locations.create_location(
            warehouse.id, StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN")
        )
        await _post_balance(
            session,
            company_id=tenant.company_id,
            warehouse_id=warehouse.id,
            location_id=bin_a.id,
            on_hand_quantity="4",
        )

        with pytest.raises(BusinessRuleError) as exc:
            await locations.delete_location(bin_a.id, reason="Rack removed")
        assert exc.value.code is ErrorCode.REFERENCED_RECORD_IN_USE

    async def test_a_bin_with_children_cannot_be_retired(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouse = await WarehouseService(session, tenant).create_warehouse(_payload("rm-01"))
        locations = StorageLocationService(session, tenant)
        rack = await locations.create_location(
            warehouse.id, StorageLocationCreate(code="rack-a", name="Rack A", location_type="RACK")
        )
        await locations.create_location(
            warehouse.id,
            StorageLocationCreate(
                code="bin-01", name="Bin 1", location_type="BIN", parent_location_id=rack.id
            ),
        )

        with pytest.raises(BusinessRuleError, match="sub-locations"):
            await locations.delete_location(rack.id, reason="Rack removed")

    async def test_an_empty_bin_is_retired_and_audited(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouse = await WarehouseService(session, tenant).create_warehouse(_payload("rm-01"))
        locations = StorageLocationService(session, tenant)
        bin_a = await locations.create_location(
            warehouse.id, StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN")
        )

        await locations.delete_location(bin_a.id, reason="Rack dismantled")

        stored = await session.get(StorageLocation, bin_a.id)
        assert stored is not None and stored.deleted_at is not None
        assert await _audit_actions(session, bin_a.id) == ["CREATE", "DELETE"]

    async def test_listing_is_scoped_to_one_warehouse(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        warehouses = WarehouseService(session, tenant)
        locations = StorageLocationService(session, tenant)
        first = await warehouses.create_warehouse(_payload("rm-01"))
        second = await warehouses.create_warehouse(
            _payload("fg-01", warehouse_type="FINISHED_GOODS")
        )
        await locations.create_location(
            first.id, StorageLocationCreate(code="bin-01", name="Bin 1", location_type="BIN")
        )
        await locations.create_location(
            second.id, StorageLocationCreate(code="bin-02", name="Bin 2", location_type="BIN")
        )

        items, pagination = await locations.list_locations(
            StorageLocationFilters(warehouse_id=second.id), PageParams(), SortParams()
        )

        assert [item.code for item in items] == ["BIN-02"]
        assert pagination.total == 1
