"""Brand and manufacturer masters: lifecycle, request shapes and the probes.

No database. Everything here is decided from a record's two flags, a request
body, or the SQL a probe compiles to - and each is reachable through the API in
the wrong order if it is wrong:

* the lifecycle is the organisation masters' machine, so a retired brand cannot
  be brought back by a PATCH and cannot be retired twice;
* a PATCH may not move a code, and may not "clear" a column that cannot be empty;
* the in-use probes are tenant-scoped and count live rows only - a probe that
  forgot either would refuse a retirement because of another company's item, or
  allow one while an item still shows the brand;
* every ``ItemSummary`` field is in the explicit list column set, the trap that
  turns an item list into a 500 the moment a schema field is added alone.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.dialects import postgresql

from src.core.clock import utcnow
from src.core.deps import TenantContext
from src.core.errors import InvalidStateTransitionError
from src.core.permissions import PERMISSION_GROUPS, ROLE_PRESETS, Permission, group_for
from src.modules.organization.service import MasterLifecycleState
from src.modules.products.repository import (
    BRAND_ENTITY,
    ITEM_SUMMARY_COLUMNS,
    MANUFACTURER_ENTITY,
    BrandRepository,
    ItemRepository,
    ManufacturerRepository,
    ManufacturerUsage,
)
from src.modules.products.schemas import (
    BrandCreate,
    BrandUpdate,
    ItemCreate,
    ItemFilters,
    ItemSummary,
    ItemSummaryWithCost,
    ItemUpdate,
    ManufacturerCreate,
    ManufacturerUpdate,
    VendorLinkCreate,
    VendorLinkUpdate,
)
from src.modules.products.service import (
    BRAND_LIFECYCLE,
    MANUFACTURER_LIFECYCLE,
    VENDOR_LINK_CLEARABLE_FIELDS,
    master_state,
)

pytestmark = pytest.mark.unit

MACHINES = pytest.mark.parametrize(
    "machine", [BRAND_LIFECYCLE, MANUFACTURER_LIFECYCLE], ids=["brand", "manufacturer"]
)


def _row(*, is_active: bool = True, deleted: bool = False) -> Any:
    return SimpleNamespace(is_active=is_active, deleted_at=utcnow() if deleted else None)


# ----------------------------------------------------------------- lifecycle --


class TestLifecycle:
    @MACHINES
    def test_the_graph_is_structurally_sound(self, machine: Any) -> None:
        # No unreachable state, and no non-terminal state without an exit.
        machine.validate()

    def test_each_machine_names_its_record_the_way_get_or_404_does(self) -> None:
        assert BRAND_LIFECYCLE.entity == BRAND_ENTITY == "Brand"
        assert MANUFACTURER_LIFECYCLE.entity == MANUFACTURER_ENTITY == "Manufacturer"
        assert BrandRepository.entity_label == BRAND_ENTITY
        assert ManufacturerRepository.entity_label == MANUFACTURER_ENTITY

    @MACHINES
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            (MasterLifecycleState.ACTIVE, MasterLifecycleState.INACTIVE),
            (MasterLifecycleState.INACTIVE, MasterLifecycleState.ACTIVE),
            (MasterLifecycleState.ACTIVE, MasterLifecycleState.ARCHIVED),
            (MasterLifecycleState.INACTIVE, MasterLifecycleState.ARCHIVED),
        ],
    )
    def test_the_legal_moves(
        self, machine: Any, source: MasterLifecycleState, target: MasterLifecycleState
    ) -> None:
        assert machine.can_transition(source, target)

    @MACHINES
    def test_retirement_is_terminal(self, machine: Any) -> None:
        """No restore endpoint exists, so no edge may advertise one."""
        assert machine.is_terminal(MasterLifecycleState.ARCHIVED)
        assert machine.allowed_targets(MasterLifecycleState.ARCHIVED) == []

    @MACHINES
    def test_retiring_twice_is_refused_not_ignored(self, machine: Any) -> None:
        with pytest.raises(InvalidStateTransitionError):
            machine.assert_transition(MasterLifecycleState.ARCHIVED, MasterLifecycleState.ARCHIVED)

    @MACHINES
    def test_a_no_op_activation_is_not_a_transition(self, machine: Any) -> None:
        # The service never asks the machine about a PATCH that restates the
        # current flag; were it to, the machine would refuse it.
        with pytest.raises(InvalidStateTransitionError):
            machine.assert_transition(MasterLifecycleState.ACTIVE, MasterLifecycleState.ACTIVE)

    def test_the_state_is_derived_from_the_two_flags(self) -> None:
        assert master_state(_row()) is MasterLifecycleState.ACTIVE
        assert master_state(_row(is_active=False)) is MasterLifecycleState.INACTIVE
        # deleted_at wins, whatever is_active says.
        assert master_state(_row(is_active=True, deleted=True)) is MasterLifecycleState.ARCHIVED


# ----------------------------------------------------------- request shapes --


class TestRequestShapes:
    def test_codes_are_normalised_so_one_brand_cannot_become_two(self) -> None:
        brand = BrandCreate(code="  atcraft   pro ", name="ATCRAFT Pro")
        maker = ManufacturerCreate(code="dm  cyl", name="Cylinder maker")

        assert brand.code == "ATCRAFT PRO"
        assert maker.code == "DM CYL"

    def test_a_manufacturer_defaults_to_india_and_blank_text_is_null(self) -> None:
        maker = ManufacturerCreate(code="M1", name="Maker", website="   ", legal_name="")

        assert maker.country == "India"
        assert maker.website is None
        assert maker.legal_name is None

    @pytest.mark.parametrize("model", [BrandUpdate, ManufacturerUpdate])
    def test_an_update_cannot_move_the_code(self, model: Any) -> None:
        """Refused, not ignored: a silently dropped field reads as success."""
        with pytest.raises(PydanticValidationError):
            model(code="NEW-CODE")

    @pytest.mark.parametrize(
        ("model", "field"),
        [
            (BrandUpdate, "name"),
            (BrandUpdate, "sort_order"),
            (BrandUpdate, "is_active"),
            (ManufacturerUpdate, "name"),
            (ManufacturerUpdate, "country"),
            (ManufacturerUpdate, "is_active"),
        ],
    )
    def test_a_column_that_cannot_be_empty_cannot_be_cleared(self, model: Any, field: str) -> None:
        with pytest.raises(PydanticValidationError) as excinfo:
            model(**{field: None})
        assert excinfo.value.errors()[0]["loc"] == (field,)

    def test_a_nullable_column_can_be_cleared_and_an_omitted_one_is_left_alone(self) -> None:
        update = BrandUpdate(description=None, manufacturer_id=None)

        assert update.model_dump(exclude_unset=True) == {
            "description": None,
            "manufacturer_id": None,
        }

    @pytest.mark.parametrize("sort_order", [-1, 10_000])
    def test_the_brand_position_is_bounded_like_the_check(self, sort_order: int) -> None:
        with pytest.raises(PydanticValidationError):
            BrandCreate(code="B", name="Brand", sort_order=sort_order)

    @pytest.mark.parametrize("model", [ItemUpdate, VendorLinkUpdate])
    def test_a_blank_part_number_is_stored_as_null(self, model: Any) -> None:
        assert model(manufacturer_part_number="   ").manufacturer_part_number is None

    def test_an_item_create_carries_the_brand_and_the_maker(self) -> None:
        brand_id, maker_id = uuid.uuid4(), uuid.uuid4()
        payload = ItemCreate(
            code="FG-1",
            name="Lever handle",
            item_group="FINISHED_GOOD",
            base_uom_id=uuid.uuid4(),
            brand_id=brand_id,
            manufacturer_id=maker_id,
            manufacturer_part_number=" MPN-42 ",
        )

        assert (payload.brand_id, payload.manufacturer_id) == (brand_id, maker_id)
        assert payload.manufacturer_part_number == "MPN-42"

    def test_a_vendor_link_names_the_maker_it_supplies(self) -> None:
        maker_id = uuid.uuid4()
        link = VendorLinkCreate(vendor_id=uuid.uuid4(), manufacturer_id=maker_id)

        assert link.manufacturer_id == maker_id
        # The two fields an explicit null clears on a link update.
        assert {"manufacturer_id", "manufacturer_part_number"} == VENDOR_LINK_CLEARABLE_FIELDS


# --------------------------------------------------------------- list shape --


class TestListProjection:
    def test_every_summary_field_is_in_the_list_column_set(self) -> None:
        """The raiseload trap: a schema field the SELECT does not name is a 500.

        The list loads ``ITEM_SUMMARY_COLUMNS`` with ``raiseload=True``, so a field
        added to ``ItemSummary`` alone makes every item list - offset and keyset
        - fail on its first row. ``brand_id`` is the field that made this worth
        asserting in general rather than for one column.
        """
        loaded = {column.key for column in ITEM_SUMMARY_COLUMNS}
        cost_only = set(ItemSummaryWithCost.model_fields) - set(ItemSummary.model_fields)

        assert set(ItemSummary.model_fields) - loaded == set()
        assert cost_only == {"standard_cost"}, "the cost column is added for cost viewers only"

    def test_the_item_search_filters_on_brand_and_maker_in_sql(self) -> None:
        tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
        repo = ItemRepository(None, tenant)  # type: ignore[arg-type]

        stmt = repo.build_search_statement(
            ItemFilters(brand_id=uuid.uuid4(), manufacturer_id=uuid.uuid4()), include_cost=False
        )
        sql = str(stmt.compile(dialect=postgresql.dialect()))

        assert "items.brand_id = " in sql
        assert "items.manufacturer_id = " in sql


# ------------------------------------------------------------------- probes --


class _Result:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalar(self) -> Any:
        return self.row

    def one(self) -> Any:
        return self.row


class _Session:
    """Records the statement and answers with a canned row."""

    def __init__(self, row: Any) -> None:
        self.row = row
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.row)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.fixture
def tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


class TestInUseProbes:
    async def test_a_brand_is_in_use_only_by_live_items_of_this_company(
        self, tenant: TenantContext
    ) -> None:
        session = _Session(row=True)

        assert await BrandRepository(session, tenant).is_used(uuid.uuid4()) is True  # type: ignore[arg-type]
        sql = _sql(session.statements[0])
        assert "EXISTS" in sql
        assert "items.company_id = " in sql
        assert "items.brand_id = " in sql
        assert "items.deleted_at IS NULL" in sql

    async def test_a_maker_is_probed_on_items_links_and_brands_in_one_statement(
        self, tenant: TenantContext
    ) -> None:
        session = _Session(row=SimpleNamespace(items=False, vendor_links=True, brands=True))

        usage = await ManufacturerRepository(session, tenant).usage(uuid.uuid4())  # type: ignore[arg-type]

        assert len(session.statements) == 1, "one round trip, not one per table"
        sql = _sql(session.statements[0])
        for table in ("items", "item_vendor_links", "brands"):
            assert f"{table}.company_id = " in sql
            assert f"{table}.manufacturer_id = " in sql
            assert f"{table}.deleted_at IS NULL" in sql
        assert usage == ManufacturerUsage(items=False, vendor_links=True, brands=True)

    async def test_the_probe_binds_the_tenant_from_the_context(self, tenant: TenantContext) -> None:
        session = _Session(row=False)
        brand_id = uuid.uuid4()

        await BrandRepository(session, tenant).is_used(brand_id)  # type: ignore[arg-type]

        params = session.statements[0].compile(dialect=postgresql.dialect()).params
        assert tenant.company_id in params.values()
        assert brand_id in params.values()

    def test_a_refusal_can_say_what_still_names_the_maker(self) -> None:
        nothing = ManufacturerUsage(items=False, vendor_links=False, brands=False)
        all_three = ManufacturerUsage(items=True, vendor_links=True, brands=True)

        assert nothing.in_use is False
        assert nothing.holders() == []
        assert all_three.in_use is True
        assert all_three.holders() == ["items", "item-vendor links", "brands"]


# -------------------------------------------------------------- permissions --


class TestPermissions:
    def test_the_four_rights_sit_in_the_item_master_group(self) -> None:
        for permission in (
            Permission.BRAND_VIEW,
            Permission.BRAND_MANAGE,
            Permission.MANUFACTURER_VIEW,
            Permission.MANUFACTURER_MANAGE,
        ):
            assert group_for(permission) == "Item Master"
        assert {"brand", "manufacturer"} <= set(PERMISSION_GROUPS["Item Master"])

    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (
                "DESIGN_ENGINEER",
                {
                    Permission.BRAND_VIEW,
                    Permission.BRAND_MANAGE,
                    Permission.MANUFACTURER_VIEW,
                    Permission.MANUFACTURER_MANAGE,
                },
            ),
            ("SALES_EXECUTIVE", {Permission.BRAND_VIEW}),
            ("PURCHASE_EXECUTIVE", {Permission.BRAND_VIEW, Permission.MANUFACTURER_VIEW}),
            ("PURCHASE_MANAGER", {Permission.BRAND_VIEW, Permission.MANUFACTURER_VIEW}),
            ("PLANNER", {Permission.BRAND_VIEW}),
        ],
    )
    def test_the_presets_hold_exactly_the_rights_their_work_needs(
        self, role: str, expected: set[Permission]
    ) -> None:
        held = ROLE_PRESETS[role]["permissions"]
        assert isinstance(held, frozenset)
        ours = {
            p
            for p in held
            if p
            in {
                Permission.BRAND_VIEW,
                Permission.BRAND_MANAGE,
                Permission.MANUFACTURER_VIEW,
                Permission.MANUFACTURER_MANAGE,
            }
        }
        assert ours == expected
