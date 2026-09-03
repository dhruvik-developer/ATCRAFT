"""Validation and normalisation of the warehouse module's payloads.

These are the rules that stop bad master data reaching the database at all:
code normalisation, the fixed type vocabularies from master rules section 9,
and the partial-update semantics the service depends on.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.modules.warehouses.models import StorageLocationType, WarehouseType
from src.modules.warehouses.repository import _BALANCE_COLUMNS, _NON_ZERO_BALANCE
from src.modules.warehouses.schemas import (
    StatusChangeRequest,
    StorageLocationCreate,
    StorageLocationUpdate,
    WarehouseCreate,
    WarehouseUpdate,
)

pytestmark = pytest.mark.unit


def _warehouse_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "code": "rm-01",
        "name": "Raw material store",
        "warehouse_type": "RAW_MATERIAL",
    }
    base.update(overrides)
    return base


class TestTypeVocabularies:
    def test_warehouse_types_are_exactly_the_eleven_from_section_9(self) -> None:
        assert [t.value for t in WarehouseType] == [
            "RAW_MATERIAL",
            "COMPONENT",
            "WIP",
            "ASSEMBLY",
            "FINISHED_GOODS",
            "PACKING",
            "SCRAP",
            "RETURN",
            "QC_HOLD",
            "JOB_WORK",
            "IN_TRANSIT",
        ]

    def test_storage_location_types_are_exactly_the_five(self) -> None:
        assert [t.value for t in StorageLocationType] == [
            "RACK",
            "BIN",
            "SHELF",
            "FLOOR",
            "STAGING",
        ]

    def test_an_invented_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WarehouseCreate(**_warehouse_payload(warehouse_type="COLD_STORE"))


class TestCodeNormalisation:
    @pytest.mark.parametrize(
        ("supplied", "stored"),
        [
            ("rm-01", "RM-01"),
            ("  rm-01  ", "RM-01"),
            ("rm   01", "RM 01"),
            ("Fg/02", "FG/02"),
        ],
    )
    def test_codes_are_canonicalised(self, supplied: str, stored: str) -> None:
        assert WarehouseCreate(**_warehouse_payload(code=supplied)).code == stored

    @pytest.mark.parametrize("bad", ["", "   ", "-RM01", "RM#01", "RM(1)"])
    def test_unusable_codes_are_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            WarehouseCreate(**_warehouse_payload(code=bad))

    def test_location_codes_use_the_same_rule(self) -> None:
        location = StorageLocationCreate(code=" a-01 ", name="Bin A1", location_type="BIN")
        assert location.code == "A-01"


class TestWarehouseCreateDefaults:
    def test_negative_stock_is_off_unless_asked_for(self) -> None:
        """The safety net is on by default; relaxing it is an explicit choice."""
        assert WarehouseCreate(**_warehouse_payload()).allow_negative_stock is False

    def test_it_can_be_switched_on_deliberately(self) -> None:
        payload = WarehouseCreate(
            **_warehouse_payload(warehouse_type="WIP", allow_negative_stock=True)
        )
        assert payload.allow_negative_stock is True

    def test_new_warehouses_are_active_and_not_default(self) -> None:
        payload = WarehouseCreate(**_warehouse_payload())
        assert payload.is_active is True
        assert payload.is_default is False

    def test_country_defaults_to_the_deployment_country(self) -> None:
        assert WarehouseCreate(**_warehouse_payload()).country == "India"

    def test_blank_optional_address_parts_become_null(self) -> None:
        payload = WarehouseCreate(**_warehouse_payload(city="   ", state="Gujarat"))
        assert payload.city is None
        assert payload.state == "Gujarat"

    def test_name_whitespace_is_collapsed(self) -> None:
        assert WarehouseCreate(**_warehouse_payload(name="  Raw   store ")).name == "Raw store"

    def test_blank_name_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            WarehouseCreate(**_warehouse_payload(name="   "))

    def test_a_malformed_email_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            WarehouseCreate(**_warehouse_payload(email="not-an-address"))

    def test_company_id_cannot_be_supplied_by_the_client(self) -> None:
        """Tenant comes from the token; a body field of that name is ignored."""
        payload = WarehouseCreate(**_warehouse_payload(company_id=str(uuid.uuid4())))
        assert not hasattr(payload, "company_id")


class TestPartialUpdateSemantics:
    def test_only_the_supplied_keys_are_reported_as_changes(self) -> None:
        changes = WarehouseUpdate(name="New name").model_dump(exclude_unset=True)
        # Without exclude_unset this PATCH would null out the manager, branch,
        # plant and the whole address block.
        assert changes == {"name": "New name"}

    def test_an_explicit_null_is_a_real_change(self) -> None:
        changes = WarehouseUpdate(manager_user_id=None).model_dump(exclude_unset=True)
        assert changes == {"manager_user_id": None}

    def test_code_cannot_be_edited(self) -> None:
        with pytest.raises(ValidationError):
            WarehouseUpdate(code="RM-02")

    def test_unknown_fields_are_rejected_rather_than_ignored(self) -> None:
        with pytest.raises(ValidationError):
            WarehouseUpdate(is_default=True)

    def test_location_code_cannot_be_edited_either(self) -> None:
        with pytest.raises(ValidationError):
            StorageLocationUpdate(code="BIN-02")


class TestLocationCapacity:
    def test_capacity_is_optional(self) -> None:
        location = StorageLocationCreate(code="BIN-1", name="Bin 1", location_type="BIN")
        assert location.capacity_quantity is None
        assert location.max_weight_kg is None

    def test_capacity_is_decimal_not_float(self) -> None:
        location = StorageLocationCreate(
            code="BIN-1", name="Bin 1", location_type="BIN", capacity_quantity="12.5"
        )
        assert location.capacity_quantity == Decimal("12.5")
        assert isinstance(location.capacity_quantity, Decimal)

    @pytest.mark.parametrize("field", ["capacity_quantity", "max_weight_kg", "max_volume_m3"])
    def test_negative_capacity_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError):
            StorageLocationCreate(code="BIN-1", name="Bin 1", location_type="BIN", **{field: "-1"})


class TestStatusChangeRequest:
    def test_a_reason_is_mandatory(self) -> None:
        with pytest.raises(ValidationError):
            StatusChangeRequest()

    def test_a_token_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            StatusChangeRequest(reason="x")

    def test_a_real_reason_is_accepted(self) -> None:
        assert StatusChangeRequest(reason="Site closed").reason == "Site closed"


class TestBalanceProbeColumns:
    """The delete guard reads inventory_balances by name, so pin the column set."""

    def test_generated_available_quantity_is_not_double_counted(self) -> None:
        # available_quantity is GENERATED from the others (database contract
        # section 4.3); including it would count the same stock twice.
        assert "available_quantity" not in _BALANCE_COLUMNS

    def test_every_stock_bucket_is_checked(self) -> None:
        assert set(_BALANCE_COLUMNS) == {
            "on_hand_quantity",
            "reserved_quantity",
            "allocated_quantity",
            "in_transit_quantity",
            "qc_hold_quantity",
            "damaged_quantity",
            "job_work_quantity",
            "scrap_quantity",
        }

    def test_the_predicate_treats_null_as_zero(self) -> None:
        # A balance row written before a column existed reads NULL, which must
        # not be mistaken for "still holding stock".
        assert _NON_ZERO_BALANCE.count("COALESCE(") == len(_BALANCE_COLUMNS)
        assert "COALESCE(on_hand_quantity, 0) <> 0" in _NON_ZERO_BALANCE
