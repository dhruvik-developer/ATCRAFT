"""What a movement completing an in-transit quantity is, and is not, held to.

A stock transfer takes stock off its source at dispatch and books it in at the
destination at receive. Between the two the goods are on a vehicle:
``TRANSFER_FLOW`` has no edge out of ``IN_TRANSIT`` except ``RECEIVED``, so a
rule that can refuse the inbound leg after the outbound leg has posted does not
protect anything - it strands the stock where no document can reach it.

The invariant is therefore not "dispatch is strict" or "receive is lenient". It
is **anything dispatch lets through, receive accepts**, and it is held up by two
things: dispatch validating the destination with exactly the rules receive
applies to it, and receive not re-applying master-data policy that may have
changed while the lorry was on the road.

These are the pure half. The half that needs a database - a transfer whose
destination goes bad between create and dispatch, and again between dispatch and
receive - is in ``tests/integration/test_inventory_transfer_stranding.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.core.errors import BusinessRuleError, ValidationError
from src.modules.inventory.models import StockState, TransactionType
from src.modules.inventory.repository import ItemFacts, LocationFacts, WarehouseFacts
from src.modules.inventory.service import MovementRequest, validate_movement

pytestmark = pytest.mark.unit


def _item(**overrides: object) -> ItemFacts:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "RM-0001",
        "name": "Brass rod",
        "is_batch_tracked": False,
        "is_serial_tracked": False,
        "is_active": True,
        "standard_cost": Decimal("40"),
        "reorder_level": Decimal("0"),
    }
    return ItemFacts(**{**defaults, **overrides})  # type: ignore[arg-type]


def _warehouse(**overrides: object) -> WarehouseFacts:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "WH-01",
        "name": "Main store",
        "is_active": True,
        "allow_negative_stock": False,
    }
    return WarehouseFacts(**{**defaults, **overrides})  # type: ignore[arg-type]


def _bin(warehouse_id: uuid.UUID, **overrides: object) -> LocationFacts:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "code": "BIN-A",
        "warehouse_id": warehouse_id,
        "is_active": True,
    }
    return LocationFacts(**{**defaults, **overrides})  # type: ignore[arg-type]


def _arrival(
    item: ItemFacts,
    warehouse: WarehouseFacts,
    *,
    completes_in_transit: bool,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
) -> MovementRequest:
    return MovementRequest(
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal("4"),
        transaction_type=TransactionType.TRANSFER_IN,
        reference_type="stock_transfer",
        reason="the goods arrived",
        location_id=location_id,
        batch_id=batch_id,
        serial_id=serial_id,
        completes_in_transit=completes_in_transit,
    )


def _validate(
    movement: MovementRequest,
    item: ItemFacts,
    warehouse: WarehouseFacts,
    location: LocationFacts | None = None,
) -> None:
    validate_movement(
        movement,
        item=item,
        warehouse=warehouse,
        location=location,
        batch_owner=None,
        serial_owner=None,
    )


class TestTheFlagIsNarrow:
    """It exempts one movement from policy, so it must not be settable anywhere
    else. A flag that any caller could pass would turn the active-flag rules
    into a suggestion."""

    def test_only_the_inbound_leg_of_a_transfer_may_carry_it(self) -> None:
        with pytest.raises(ValidationError, match="inbound leg"):
            MovementRequest(
                item_id=uuid.uuid4(),
                warehouse_id=uuid.uuid4(),
                quantity=Decimal("1"),
                transaction_type=TransactionType.PURCHASE_RECEIPT,
                reference_type="goods_receipt",
                reason="not a transfer",
                completes_in_transit=True,
            )

    @pytest.mark.parametrize(
        "transaction_type",
        sorted(set(TransactionType) - {TransactionType.TRANSFER_IN}, key=str),
    )
    def test_no_other_type_at_all(self, transaction_type: TransactionType) -> None:
        with pytest.raises(ValidationError):
            MovementRequest(
                item_id=uuid.uuid4(),
                warehouse_id=uuid.uuid4(),
                quantity=Decimal("1"),
                transaction_type=transaction_type,
                reference_type="whatever",
                reason="not a transfer",
                completes_in_transit=True,
            )

    def test_a_transfer_leg_that_does_not_set_it_is_still_ordinary(self) -> None:
        """``transfer_stock`` posts both legs in one transaction, so refusing
        its inbound leg strands nothing. It keeps the strict rule."""
        item = _item(is_active=False)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError, match="inactive"):
            _validate(_arrival(item, warehouse, completes_in_transit=False), item, warehouse)


class TestPolicyDoesNotStrandGoodsThatHaveAlreadyLeft:
    """The three active flags and the two tracking flags, each of which could be
    flipped while a lorry was on the road."""

    def test_an_item_deactivated_in_flight_does_not_refuse_the_arrival(self) -> None:
        item = _item(is_active=False)
        warehouse = _warehouse()
        _validate(_arrival(item, warehouse, completes_in_transit=True), item, warehouse)

    def test_a_warehouse_deactivated_in_flight_does_not_refuse_the_arrival(self) -> None:
        item = _item()
        warehouse = _warehouse(is_active=False)
        _validate(_arrival(item, warehouse, completes_in_transit=True), item, warehouse)

    def test_a_bin_deactivated_in_flight_does_not_refuse_the_arrival(self) -> None:
        item = _item()
        warehouse = _warehouse()
        retired = _bin(warehouse.id, is_active=False)
        _validate(
            _arrival(item, warehouse, completes_in_transit=True, location_id=retired.id),
            item,
            warehouse,
            retired,
        )

    def test_batch_tracking_switched_on_in_flight_does_not_refuse_the_arrival(self) -> None:
        """The quantity left its source without a lot, because the flag did not
        exist yet. Refusing its arrival creates no traceability - it only leaves
        the stock nowhere."""
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        _validate(_arrival(item, warehouse, completes_in_transit=True), item, warehouse)

    def test_serial_tracking_switched_on_in_flight_does_not_refuse_the_arrival(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        _validate(_arrival(item, warehouse, completes_in_transit=True), item, warehouse)

    def test_all_of_them_at_once(self) -> None:
        """The realistic version: a store is retired, and everything in it is
        deactivated on the same afternoon."""
        item = _item(is_active=False, is_batch_tracked=True, is_serial_tracked=True)
        warehouse = _warehouse(is_active=False)
        retired = _bin(warehouse.id, is_active=False)
        _validate(
            _arrival(item, warehouse, completes_in_transit=True, location_id=retired.id),
            item,
            warehouse,
            retired,
        )


class TestPositionIntegrityStillHolds:
    """The rules that are not policy. Breaking one of these loses the stock in a
    way no operator can undo, so an arrival is held to them exactly like any
    other movement."""

    def test_a_bin_in_a_different_warehouse_is_still_refused(self) -> None:
        item = _item()
        warehouse = _warehouse()
        wrong = _bin(uuid.uuid4())
        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            _validate(
                _arrival(item, warehouse, completes_in_transit=True, location_id=wrong.id),
                item,
                warehouse,
                wrong,
            )

    def test_a_batch_belonging_to_another_item_is_still_refused(self) -> None:
        item = _item(is_batch_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError, match="different item"):
            validate_movement(
                _arrival(item, warehouse, completes_in_transit=True, batch_id=uuid.uuid4()),
                item=item,
                warehouse=warehouse,
                batch_owner=(uuid.uuid4(), "L-2026-07"),
                serial_owner=None,
            )

    def test_a_serial_belonging_to_another_item_is_still_refused(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        with pytest.raises(BusinessRuleError, match="different item"):
            validate_movement(
                _arrival(item, warehouse, completes_in_transit=True, serial_id=uuid.uuid4()),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=(uuid.uuid4(), "SN-0001"),
            )

    def test_a_serial_movement_of_more_than_one_unit_is_still_refused(self) -> None:
        item = _item(is_serial_tracked=True)
        warehouse = _warehouse()
        serial_id = uuid.uuid4()
        with pytest.raises(BusinessRuleError, match="single unit"):
            validate_movement(
                _arrival(item, warehouse, completes_in_transit=True, serial_id=serial_id),
                item=item,
                warehouse=warehouse,
                batch_owner=None,
                serial_owner=(item.id, "SN-0001"),
            )


class TestTheOrdinaryRulesAreUnchanged:
    """The exemption is one flag on one movement. Nothing else moved."""

    @pytest.mark.parametrize(
        "broken",
        [
            {"is_active": False},
            {"is_batch_tracked": True},
            {"is_serial_tracked": True},
        ],
        ids=["inactive-item", "batch-required", "serial-required"],
    )
    def test_a_plain_receipt_is_still_refused(self, broken: dict[str, object]) -> None:
        item = _item(**broken)
        warehouse = _warehouse()
        movement = MovementRequest(
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("4"),
            transaction_type=TransactionType.PURCHASE_RECEIPT,
            reference_type="goods_receipt",
            reason="ordinary arrival",
        )
        with pytest.raises(BusinessRuleError):
            _validate(movement, item, warehouse)

    def test_an_outbound_movement_of_an_inactive_item_still_passes(self) -> None:
        """Unchanged, and the reason the exemption is coherent: a deactivated
        item has always been allowed to give stock up."""
        item = _item(is_active=False)
        warehouse = _warehouse(is_active=False)
        movement = MovementRequest(
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("4"),
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type="material_issue",
            reason="the flag does not trap stock",
            stock_state=StockState.AVAILABLE,
        )
        _validate(movement, item, warehouse)
