"""Batch and serial tracking against a real PostgreSQL.

The register has two different jobs, so the tests are grouped that way.

A **lot** has a lifecycle and a balance: quarantining it must move the stock
that can still be picked, expiring it must stop the line rather than merely
change a word on a screen, and an issue that leaves the last unit must close
the lot without anyone remembering to. FEFO is tested through the engine, not
through a helper, because the whole point is that an operator who names no lot
gets the one that expires first.

A **serial** is one physical object with a position. A transfer must park the
named units in transit and land those same units at the destination, and a
write-off must retire the named units rather than an anonymous quantity. Those
are the two paths where a quantity-only model would look correct on the balance
and be wrong on the floor.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.core.clock import today
from src.core.errors import BusinessRuleError
from src.modules.audit.models import AuditLog
from src.modules.inventory.models import (
    BatchStatus,
    InventoryTransaction,
    SerialStatus,
    StockSerial,
    TransactionType,
)
from src.modules.inventory.schemas import (
    MAX_SERIAL_BULK,
    StockAdjustmentCreate,
    StockBatchAction,
    StockBatchCreate,
    StockSerialBulkCreate,
    StockTransferCreate,
)
from src.modules.inventory.service import (
    MovementRequest,
    StockAdjustmentService,
    StockTransferService,
    TraceabilityService,
    get_balance,
    post_movement,
    post_movements,
)
from tests.seed_support import seed_item, seed_warehouse
from tests.statement_support import counted, selects

pytestmark = pytest.mark.integration


async def _receive(
    session,
    tenant,
    item,
    warehouse,
    quantity: str,
    cost: str = "10",
    *,
    batch_id=None,
    serial_id=None,
) -> InventoryTransaction:
    return await post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="test",
        reference_id=None,
        reference_code=None,
        reason="Opening test stock",
        unit_cost=Decimal(cost),
        batch_id=batch_id,
        serial_id=serial_id,
    )


async def _lot(session, tenant, item, *, number: str, expires_on: date | None = None):
    return await TraceabilityService(session, tenant).create_batch(
        StockBatchCreate(
            item_id=item.id,
            batch_number=number,
            received_on=today(),
            expires_on=expires_on,
        )
    )


# --------------------------------------------------------------------- lots


class TestBatchLifecycle:
    async def test_quarantine_moves_available_stock_to_qc_hold_and_expiry_blocks_issue(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("LOT"),
            is_batch_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        batch = await _lot(session, tenant, item, number=unique_code("L"))
        await _receive(session, tenant, item, warehouse, "4", batch_id=batch.id)

        trace = TraceabilityService(session, tenant)
        held = await trace.quarantine_batch(
            batch.id, StockBatchAction(reason="Damaged carton at goods-in")
        )
        assert held.status is BatchStatus.QUARANTINED

        balance = await get_balance(session, tenant, item.id, warehouse.id, batch_id=batch.id)
        assert balance is not None
        assert (balance.available_quantity, balance.qc_hold_quantity) == (0, 4)
        with pytest.raises(BusinessRuleError, match="quarantined"):
            await post_movement(
                session,
                tenant,
                item_id=item.id,
                warehouse_id=warehouse.id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="test",
                reference_id=None,
                reference_code=None,
                reason="Try to consume held stock",
                batch_id=batch.id,
            )

        released = await trace.activate_batch(
            batch.id, StockBatchAction(reason="Quality sample passed")
        )
        assert released.status is BatchStatus.ACTIVE
        still_held = await get_balance(session, tenant, item.id, warehouse.id, batch_id=batch.id)
        assert still_held is not None
        # Releasing a lot is a quality decision, not a warehouse movement: the
        # stock deliberately stays in QC hold until a QC release is posted.
        assert (still_held.available_quantity, still_held.qc_hold_quantity) == (0, 4)

    async def test_an_expired_lot_cannot_be_issued_and_an_empty_lot_is_closed(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("EXP"),
            is_batch_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        trace = TraceabilityService(session, tenant)
        expiring = await _lot(
            session,
            tenant,
            item,
            number=unique_code("OLD"),
            expires_on=today() + timedelta(days=30),
        )
        await _receive(session, tenant, item, warehouse, "2", batch_id=expiring.id)
        expired = await trace.expire_batch(
            expiring.id, StockBatchAction(reason="Past its shelf life")
        )
        assert expired.status is BatchStatus.EXPIRED
        with pytest.raises(BusinessRuleError, match="expired"):
            await post_movement(
                session,
                tenant,
                item_id=item.id,
                warehouse_id=warehouse.id,
                quantity=Decimal("1"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="test",
                reference_id=None,
                reference_code=None,
                reason="Try to issue expired stock",
                batch_id=expiring.id,
            )

        empty = await _lot(session, tenant, item, number=unique_code("EMPTY"))
        await _receive(session, tenant, item, warehouse, "3", batch_id=empty.id)
        issue = await post_movement(
            session,
            tenant,
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("3"),
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type="test",
            reference_id=None,
            reference_code=None,
            reason="Use up the lot",
            batch_id=empty.id,
        )
        assert issue.batch_id == empty.id
        closed = await trace.get_batch(empty.id)
        assert closed.status is BatchStatus.CONSUMED

    async def test_an_issue_without_a_lot_takes_the_earliest_expiry(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("FEFO"),
            is_batch_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        later = await _lot(
            session,
            tenant,
            item,
            number=unique_code("LATER"),
            expires_on=today() + timedelta(days=60),
        )
        sooner = await _lot(
            session,
            tenant,
            item,
            number=unique_code("SOON"),
            expires_on=today() + timedelta(days=10),
        )
        # Receipt order is deliberately the opposite of expiry order: FEFO
        # must not be confused with FIFO at the lot level.
        await _receive(session, tenant, item, warehouse, "5", batch_id=later.id)
        await _receive(session, tenant, item, warehouse, "2", batch_id=sooner.id)

        issue = await post_movement(
            session,
            tenant,
            item_id=item.id,
            warehouse_id=warehouse.id,
            quantity=Decimal("2"),
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type="test",
            reference_id=None,
            reference_code=None,
            reason="Consume without naming a lot",
        )
        assert issue.batch_id == sooner.id
        trace = TraceabilityService(session, tenant)
        assert (await trace.get_batch(sooner.id)).status is BatchStatus.CONSUMED
        assert (await trace.get_batch(later.id)).status is BatchStatus.ACTIVE

    async def test_lots_emptied_together_are_locked_and_closed_in_one_pass(
        self, session, tenant, engine, unique_code
    ) -> None:
        """Closing the lots a posting emptied is one locking read, not one per lot.

        The posting still holds its balance locks while it closes them, so a
        per-lot round trip there is a per-lot wait for every order queued behind
        it. Both lots must still be closed, each with its own lifecycle row.
        """
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("LOTS"),
            is_batch_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        lots = [await _lot(session, tenant, item, number=unique_code("LOT")) for _ in range(2)]
        for lot in lots:
            await _receive(session, tenant, item, warehouse, "2", batch_id=lot.id)

        with counted(engine) as statements:
            await post_movements(
                session,
                tenant,
                [
                    MovementRequest(
                        item_id=item.id,
                        warehouse_id=warehouse.id,
                        quantity=Decimal("2"),
                        transaction_type=TransactionType.MATERIAL_ISSUE,
                        reference_type="test",
                        reference_id=None,
                        reference_code=None,
                        reason="Use up both lots",
                        batch_id=lot.id,
                    )
                    for lot in lots
                ],
            )

        lot_locks = [
            text for text in statements if "FROM stock_batches" in text and "FOR UPDATE" in text
        ]
        assert len(lot_locks) == 1, lot_locks
        trace = TraceabilityService(session, tenant)
        for lot in lots:
            assert (await trace.get_batch(lot.id)).status is BatchStatus.CONSUMED
        closed = (
            await session.execute(
                select(AuditLog.entity_id).where(
                    AuditLog.company_id == tenant.company_id,
                    AuditLog.entity_id.in_([lot.id for lot in lots]),
                    AuditLog.summary.contains("CONSUMED"),
                )
            )
        ).scalars()
        assert sorted(closed) == sorted(lot.id for lot in lots)

    async def test_lotless_issue_lines_find_their_lots_in_one_statement(
        self, session, tenant, engine, unique_code
    ) -> None:
        """Two lotless lines and six cost the same reads; each still takes FEFO's lot.

        The candidate lots were read once per lotless line inside the posting
        transaction - a two-hundred-line issue was two hundred extra round
        trips - and the item facts were read for FEFO and again for validation.
        """
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("PICK"),
            is_batch_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        later = await _lot(
            session,
            tenant,
            item,
            number=unique_code("LATER"),
            expires_on=today() + timedelta(days=60),
        )
        sooner = await _lot(
            session,
            tenant,
            item,
            number=unique_code("SOON"),
            expires_on=today() + timedelta(days=10),
        )
        await _receive(session, tenant, item, warehouse, "20", batch_id=later.id)
        await _receive(session, tenant, item, warehouse, "20", batch_id=sooner.id)

        def issue(count: int) -> list[MovementRequest]:
            return [
                MovementRequest(
                    item_id=item.id,
                    warehouse_id=warehouse.id,
                    quantity=Decimal("1"),
                    transaction_type=TransactionType.MATERIAL_ISSUE,
                    reference_type="test",
                    reference_id=None,
                    reference_code=None,
                    reason="Issue without naming a lot",
                )
                for _ in range(count)
            ]

        with counted(engine) as two_lines:
            first = await post_movements(session, tenant, issue(2))
        with counted(engine) as six_lines:
            second = await post_movements(session, tenant, issue(6))

        assert selects(six_lines) == selects(two_lines)
        assert {row.batch_id for row in [*first, *second]} == {sooner.id}

    async def test_the_expiry_sweep_expires_the_past_and_warns_about_the_near(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("SWEEP"),
            is_batch_tracked=True,
        )
        trace = TraceabilityService(session, tenant)
        past = await _lot(
            session,
            tenant,
            item,
            number=unique_code("PAST"),
            expires_on=today() - timedelta(days=3),
        )
        near = await _lot(
            session,
            tenant,
            item,
            number=unique_code("NEAR"),
            expires_on=today() + timedelta(days=10),
        )
        far = await _lot(
            session,
            tenant,
            item,
            number=unique_code("FAR"),
            expires_on=today() + timedelta(days=90),
        )

        result = await trace.sweep_expiry(warn_days=30)

        assert set(result.expired_batch_ids) == {past.id}
        assert set(result.warning_batch_ids) == {near.id}
        assert (await trace.get_batch(past.id)).status is BatchStatus.EXPIRED
        assert (await trace.get_batch(near.id)).status is BatchStatus.ACTIVE
        assert far.id not in result.warning_batch_ids


# ------------------------------------------------------------------- serials


class TestSerialMovement:
    async def _two_units(self, session, tenant, unique_code, *, item_code: str):
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code(item_code),
            is_serial_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        trace = TraceabilityService(session, tenant)
        registered = await trace.bulk_create_serials(
            StockSerialBulkCreate(
                item_id=item.id,
                serial_numbers=[unique_code("SN"), unique_code("SN")],
                warehouse_id=warehouse.id,
                unit_cost=Decimal("25"),
                received_on=today(),
            )
        )
        for unit in registered:
            await _receive(
                session,
                tenant,
                item,
                warehouse,
                "1",
                "25",
                serial_id=unit.id,
            )
        return item, warehouse, registered

    async def test_a_transfer_parks_named_units_in_transit_and_lands_them(
        self, session, tenant, unique_code
    ) -> None:
        source = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("SRC")
        )
        destination = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("DST")
        )
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("SR"),
            is_serial_tracked=True,
        )
        trace = TraceabilityService(session, tenant)
        units = await trace.bulk_create_serials(
            StockSerialBulkCreate(
                item_id=item.id,
                serial_numbers=[unique_code("SN"), unique_code("SN")],
                warehouse_id=source.id,
                unit_cost=Decimal("31"),
            )
        )
        for unit in units:
            await _receive(session, tenant, item, source, "1", "31", serial_id=unit.id)
        serial_ids = [unit.id for unit in units]

        transfers = StockTransferService(session, tenant)
        transfer = await transfers.create(
            StockTransferCreate.model_validate(
                {
                    "from_warehouse_id": str(source.id),
                    "to_warehouse_id": str(destination.id),
                    "reason": "Move the repaired pumps to the dispatch store",
                    "lines": [
                        {
                            "item_id": str(item.id),
                            "quantity": "2",
                            "serial_ids": [str(value) for value in serial_ids],
                        }
                    ],
                }
            )
        )
        await transfers.approve(transfer.id)
        await transfers.dispatch(transfer.id)

        for serial_id in serial_ids:
            assert (await trace.get_serial(serial_id)).serial.status is SerialStatus.IN_TRANSIT
        in_transit = await get_balance(session, tenant, item.id, destination.id)
        assert in_transit is not None and in_transit.in_transit_quantity == 2

        await transfers.receive(transfer.id)
        for serial_id in serial_ids:
            unit = (await trace.get_serial(serial_id)).serial
            assert (unit.status, unit.warehouse_id) == (SerialStatus.IN_STOCK, destination.id)
        arrived = await get_balance(session, tenant, item.id, destination.id)
        source_balance = await get_balance(session, tenant, item.id, source.id)
        assert arrived is not None and arrived.on_hand_quantity == 2
        assert source_balance is None or source_balance.on_hand_quantity == 0

        legs = list(
            (
                await session.execute(
                    select(InventoryTransaction).where(
                        InventoryTransaction.reference_id == transfer.id
                    )
                )
            )
            .scalars()
            .all()
        )
        # One movement per unit on each leg, not one aggregate movement with a
        # quantity of two: a serial trace has to find the unit in the ledger.
        outbound = [leg for leg in legs if leg.transaction_type is TransactionType.TRANSFER_OUT]
        inbound = [leg for leg in legs if leg.transaction_type is TransactionType.TRANSFER_IN]
        assert len(outbound) == len(inbound) == 2

    async def test_a_transfer_can_name_units_by_their_printed_labels(
        self, session, tenant, unique_code
    ) -> None:
        """The stock screen types labels; the API resolves them to unit ids.

        This is the exact payload shape `CreateTransferDialog` sends, so a
        regression in label resolution shows up as an unusable transfer screen
        rather than passing because the test happened to know the UUIDs.
        """
        item, source, units = await self._two_units(session, tenant, unique_code, item_code="LBL")
        destination = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("DST")
        )
        transfer = await StockTransferService(session, tenant).create(
            StockTransferCreate.model_validate(
                {
                    "from_warehouse_id": str(source.id),
                    "to_warehouse_id": str(destination.id),
                    "reason": "Move the labelled pumps to the dispatch store",
                    "lines": [
                        {
                            "item_id": str(item.id),
                            "quantity": "2",
                            "serial_numbers": [unit.serial_number for unit in units],
                        }
                    ],
                }
            )
        )
        assert set(transfer.lines[0].serial_ids or []) == {unit.id for unit in units}

    async def test_a_transfer_refuses_an_unregistered_label(
        self, session, tenant, unique_code
    ) -> None:
        """Typing a label is a reference to stock, not permission to mint a unit."""
        item, source, _units = await self._two_units(session, tenant, unique_code, item_code="LBLX")
        destination = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("DST")
        )
        payload = StockTransferCreate.model_validate(
            {
                "from_warehouse_id": str(source.id),
                "to_warehouse_id": str(destination.id),
                "reason": "Move a unit whose label cannot be read",
                "lines": [
                    {
                        "item_id": str(item.id),
                        "quantity": "1",
                        "serial_numbers": ["NOT-A-REGISTERED-UNIT"],
                    }
                ],
            }
        )
        with pytest.raises(BusinessRuleError, match="is not registered"):
            await StockTransferService(session, tenant).create(payload)

    async def test_a_transfer_refuses_a_unit_named_by_id_and_label(
        self, session, tenant, unique_code
    ) -> None:
        """The document validators see two shapes; the resolved ids see one unit."""
        item, source, units = await self._two_units(session, tenant, unique_code, item_code="LBLD")
        destination = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("DST")
        )
        payload = StockTransferCreate.model_validate(
            {
                "from_warehouse_id": str(source.id),
                "to_warehouse_id": str(destination.id),
                "reason": "Move the same unit on two transfer lines",
                "lines": [
                    {
                        "item_id": str(item.id),
                        "quantity": "1",
                        "serial_ids": [str(units[0].id)],
                    },
                    {
                        "item_id": str(item.id),
                        "quantity": "1",
                        # A different batch defeats the document tuple check
                        # without making the second line a different unit.
                        "batch_id": str(uuid.uuid4()),
                        "serial_numbers": [units[0].serial_number],
                    },
                ],
            }
        )
        with pytest.raises(BusinessRuleError, match="same unit"):
            await StockTransferService(session, tenant).create(payload)

    async def test_a_write_off_retires_the_named_unit_only(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse, units = await self._two_units(
            session, tenant, unique_code, item_code="ADJ"
        )
        retired = units[0].id
        adjustments = StockAdjustmentService(session, tenant)
        draft = await adjustments.create(
            StockAdjustmentCreate.model_validate(
                {
                    "warehouse_id": str(warehouse.id),
                    "reason_code": "DAMAGE",
                    "reason": "Cracked housing found in the rack",
                    "lines": [
                        {
                            "item_id": str(item.id),
                            "quantity": "-1",
                            "serial_ids": [str(retired)],
                        }
                    ],
                }
            )
        )
        assert draft.total_value == Decimal("25.00")
        posted = await adjustments.post(draft.id)
        assert posted.status == "POSTED"

        trace = TraceabilityService(session, tenant)
        assert (await trace.get_serial(retired)).serial.status is SerialStatus.SCRAPPED
        survivor = units[1].id
        assert (await trace.get_serial(survivor)).serial.status is SerialStatus.IN_STOCK
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None and balance.on_hand_quantity == 1

    async def test_a_write_off_can_name_the_unit_by_its_printed_label(
        self, session, tenant, unique_code
    ) -> None:
        """The adjustment dialog sends labels, not unit UUIDs."""
        item, warehouse, units = await self._two_units(
            session, tenant, unique_code, item_code="ADJL"
        )
        retired = units[0]
        draft = await StockAdjustmentService(session, tenant).create(
            StockAdjustmentCreate.model_validate(
                {
                    "warehouse_id": str(warehouse.id),
                    "reason_code": "DAMAGE",
                    "reason": "Cracked housing found in the rack",
                    "lines": [
                        {
                            "item_id": str(item.id),
                            "quantity": "-1",
                            "serial_numbers": [retired.serial_number],
                        }
                    ],
                }
            )
        )
        assert draft.lines[0].serial_ids == [retired.id]


class TestSerialRegistration:
    async def test_a_document_may_mint_more_units_than_the_label_desk_takes(
        self, session, tenant, unique_code
    ) -> None:
        """``MAX_SERIAL_BULK`` bounds the label desk's request, not a completion.

        Production and assembly completions, goods receipts and job-work
        receipts name their new units through ``resolve_serial_ids``. It used to
        build the label desk's request model for them, so a 501-unit run - which
        the completion's own schema allows - failed validation inside the
        service: an unhandled error, a 500, and nothing telling the operator
        what was wrong. A unit printed ahead of the goods is still adopted, and
        the ids come back in the order the document named the labels.
        """
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("RUN"),
            is_serial_tracked=True,
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        labels = [f"{item.code}-{index:04d}" for index in range(MAX_SERIAL_BULK + 1)]
        trace = TraceabilityService(session, tenant)
        [printed] = await trace.bulk_create_serials(
            StockSerialBulkCreate(item_id=item.id, serial_numbers=[labels[7]])
        )

        ids = await trace.resolve_serial_ids(
            item.id,
            labels,
            batch_id=None,
            warehouse_id=warehouse.id,
            location_id=None,
            received_on=today(),
            source="TEST/RUN",
        )

        assert len(set(ids)) == len(labels)
        assert ids[7] == printed.id
        named = dict(
            (
                await session.execute(
                    select(StockSerial.id, StockSerial.serial_number).where(StockSerial.id.in_(ids))
                )
            ).all()
        )
        assert [named[serial_id] for serial_id in ids] == labels

    async def test_a_label_run_writes_its_audit_rows_in_one_statement(
        self, session, tenant, engine, unique_code
    ) -> None:
        """Every unit is audited, and the audit rows cost one round trip per run.

        Receipts and completions mint their units through the same registration,
        so a per-unit flush there was thousands of sequential inserts on one
        large assembly.
        """
        item = await seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("LBL"),
            is_serial_tracked=True,
        )
        trace = TraceabilityService(session, tenant)

        def audit_inserts(statements: list[str]) -> int:
            return sum(
                1
                for text in statements
                if text.lstrip().upper().startswith("INSERT INTO AUDIT_LOGS")
            )

        with counted(engine) as small:
            await trace.bulk_create_serials(
                StockSerialBulkCreate(
                    item_id=item.id, serial_numbers=[unique_code("SN") for _ in range(3)]
                )
            )
        with counted(engine) as large:
            run = await trace.bulk_create_serials(
                StockSerialBulkCreate(
                    item_id=item.id, serial_numbers=[unique_code("SN") for _ in range(30)]
                )
            )

        assert audit_inserts(small) == audit_inserts(large) == 1
        audited = (
            await session.execute(
                select(AuditLog.entity_id).where(
                    AuditLog.company_id == tenant.company_id,
                    AuditLog.entity_id.in_([unit.id for unit in run]),
                )
            )
        ).scalars()
        assert sorted(audited) == sorted(unit.id for unit in run)


def test_bulk_registration_is_bounded_and_normalises_labels() -> None:
    with pytest.raises(ValidationError):
        StockSerialBulkCreate.model_validate(
            {
                "item_id": "00000000-0000-0000-0000-000000000001",
                "serial_numbers": [f"SN-{index}" for index in range(MAX_SERIAL_BULK + 1)],
            }
        )
    payload = StockSerialBulkCreate.model_validate(
        {
            "item_id": "00000000-0000-0000-0000-000000000001",
            "serial_numbers": [" sn 001 ", "SN-002"],
        }
    )
    assert payload.serial_numbers == ["SN001", "SN-002"]
    with pytest.raises(ValidationError):
        StockSerialBulkCreate.model_validate(
            {
                "item_id": "00000000-0000-0000-0000-000000000001",
                "serial_numbers": ["SN-001", "sn-001"],
            }
        )
