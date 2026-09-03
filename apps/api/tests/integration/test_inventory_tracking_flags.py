"""Turning batch or serial tracking on must never make stock immovable.

The reproduction, against real PostgreSQL:

    Item I is not batch tracked. Ten units at W1. A four-unit transfer W1 -> W2
    is created, approved and dispatched, so the quantity is off W1's on-hand and
    standing in W2's ``in_transit_quantity``. ``I.is_batch_tracked`` is then
    switched on - an ordinary item-master edit. ``receive`` books the goods in,
    because it must: ``TRANSFER_FLOW`` has no edge out of IN_TRANSIT except
    RECEIVED, and refusing the arrival would strand the lorry-load.

    Four units are then sitting at ``(item, W2, bin=NULL, batch=NULL)`` with
    ``on_hand = 4`` and ``available = 4``, and **every route out was closed**:

    * a movement naming no batch was refused by the batch-required rule, which
      was not gated by direction and so applied to departures as well;
    * a movement naming a batch keys a *different* ``BalanceGrain`` - ``batch_id``
      is part of the grain - which holds nothing.

    Owned, on the balance sheet, and immovable, with no error that says why.

Two separable things were wrong and both are pinned down here.

**The BATCH rule was direction-blind.** A batch is assigned when goods arrive;
refusing to let batch-less stock *out* does not make it batch tracked, it makes
it unreachable. So the batch rule is gated on ``gates_policy`` exactly as the
three active flags beside it are - and the grain is what keeps that safe: a
batch-less issue can only ever draw on the batch-less position, which is
precisely the stock that predates the flag.

**The SERIAL rule is deliberately not gated the same way**, because ``serial_id``
is not part of ``BalanceGrain``. A serial-less issue therefore draws on the whole
position, identified units included, choosing which by nobody's decision - stock
leaves while every ``stock_serials`` row still says IN_STOCK, and the unit can be
issued again. It is gated instead on ``SERIAL_MOVEMENT_TARGETS``, the same
mapping that decides the unit's next status, so exactly the movements that change
whose hands hold a unit must name it, and a pure pool move still cannot be
trapped. Untraced legacy quantity is refused until somebody identifies it, which
is a recoverable operator action rather than stranding - proved in
``TestTheSerialTwinOfIt``.

**The flag could be flipped under live stock.** ``update_item`` applied no
guard at all. It now refuses while the item holds anything anywhere - including
the in-transit and job-work quantities an in-flight document has already moved
out of on-hand, which is what makes the sequence above unreachable through the
API in the first place.

The two are tested separately on purpose. The guard is the prevention and the
gate is the cure, and a database restored from before the guard existed still
needs the cure - so the reproduction here reaches past ``update_item`` and
writes the flag through the ORM, which is exactly the state such a row is in.

Isolation uses the shared ``session`` fixture from ``tests/conftest.py``:
nothing here needs a second tenant or a schema of its own.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError, ErrorCode, InsufficientStockError
from src.modules.inventory import service as inv
from src.modules.inventory.models import TransactionType, TransferStatus
from src.modules.inventory.schemas import (
    StockBatchCreate,
    StockSerialCreate,
    StockTransferCreate,
    StockTransferLineCreate,
)
from src.modules.products import service as products
from src.modules.products.models import Item
from src.modules.products.repository import ItemRepository
from src.modules.products.schemas import ItemUpdate
from tests import seed_support

pytestmark = pytest.mark.integration


def seeded(session: AsyncSession):
    """Release fixture rows into the connection's transaction.

    ``transaction(...)`` rolls the session back when the block it wraps raises,
    and with ``join_transaction_mode="create_savepoint"`` that reaches every row
    the test has flushed so far - fixtures included. Most cases here provoke a
    refusal and then keep asserting against what they seeded.
    """
    return transaction(session)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


class Bench:
    def __init__(self, item_id: uuid.UUID, source: uuid.UUID, destination: uuid.UUID) -> None:
        self.item_id = item_id
        self.source = source
        self.destination = destination


async def seed_bench(session: AsyncSession, tenant: TenantContext, *, on_hand: str = "10") -> Bench:
    """One untracked item, two warehouses, ten on hand at the source."""
    async with seeded(session):
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
            can_purchase=True,
        )
        source = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WA")
        )
        destination = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WB")
        )
    bench = Bench(item.id, source.id, destination.id)
    if Decimal(on_hand) > 0:
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=bench.item_id,
                warehouse_id=bench.source,
                quantity=Decimal(on_hand),
                transaction_type=TransactionType.OPENING_BALANCE,
                reference_type="opening",
                reference_id=None,
                reference_code="OPENING",
                reason="tracking flag bench",
                unit_cost=Decimal("40"),
            )
    return bench


async def start_tracking(session: AsyncSession, item_id: uuid.UUID, field: str) -> None:
    """Flip a tracking flag *past* the item-master guard.

    Writing the column directly is the point: it reproduces the row shape a
    database already in production can be in - the flip happened before the
    guard existed - and it keeps this file measuring the movement engine rather
    than the guard, which has its own cases below.
    """
    async with seeded(session):
        item = (await session.execute(select(Item).where(Item.id == item_id))).scalar_one()
        setattr(item, field, True)


async def dispatched_transfer(
    session: AsyncSession, tenant: TenantContext, bench: Bench, *, quantity: str = "4"
) -> uuid.UUID:
    service = inv.StockTransferService(session, tenant)
    transfer = await service.create(
        StockTransferCreate(
            from_warehouse_id=bench.source,
            to_warehouse_id=bench.destination,
            reason="move stock between the stores",
            lines=[StockTransferLineCreate(item_id=bench.item_id, quantity=Decimal(quantity))],
        )
    )
    transfer_id = transfer.id
    await service.approve(transfer_id)
    await service.dispatch(transfer_id)
    return transfer_id


async def issue_out(
    session: AsyncSession,
    tenant: TenantContext,
    bench: Bench,
    *,
    quantity: str = "4",
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
) -> None:
    """The route out of the destination position. No batch, no serial."""
    async with transaction(session):
        await inv.post_movement(
            session,
            tenant,
            item_id=bench.item_id,
            warehouse_id=bench.destination,
            quantity=Decimal(quantity),
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type="material_issue",
            reference_id=None,
            reference_code=_code("MI"),
            reason="the stock has to be able to leave",
            batch_id=batch_id,
            serial_id=serial_id,
        )


# -----------------------------------------------------------------------------
# The reproduction
# -----------------------------------------------------------------------------


class TestStockStrandedByBatchTrackingSwitchedOnMidFlight:
    """The reported defect, end to end, and the assertion that closes it."""

    async def test_the_goods_arrive_at_a_batch_less_position(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Round 4's fix, restated: the arrival is not refused. That part is
        right and stays - the alternative is a lorry-load nobody can book in."""
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_batch_tracked")

        received = await inv.StockTransferService(session, tenant).receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

        landed = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert landed is not None
        assert landed.batch_id is None
        assert landed.on_hand_quantity == Decimal("4.0000")
        assert landed.in_transit_quantity == Decimal("0.0000")
        assert landed.available_quantity == Decimal("4.0000")

    async def test_and_the_four_units_can_still_be_moved_out(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The defect. Before the fix this raised BATCH_REQUIRED and the stock
        was immovable: the only other route out names a batch, which keys a
        different grain holding nothing."""
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_batch_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        await issue_out(session, tenant, bench)

        emptied = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert emptied is not None
        assert emptied.on_hand_quantity == Decimal("0.0000")
        assert emptied.available_quantity == Decimal("0.0000")

    async def test_naming_a_batch_is_not_the_escape_route(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Why the direction gate is the fix rather than "just name a lot".

        ``batch_id`` is part of the balance grain, so an issue that names one
        addresses a *different* position - a brand new, empty one. It cannot
        reach the quantity that arrived without a lot, and it never could.
        """
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_batch_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        async with seeded(session):
            batch = await inv.TraceabilityService(session, tenant).create_batch(
                StockBatchCreate(item_id=bench.item_id, batch_number=_code("L"))
            )
        batch_id = batch.id

        with pytest.raises(InsufficientStockError):
            await issue_out(session, tenant, bench, batch_id=batch_id)

        # And the untraced quantity is still exactly where it was.
        landed = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert landed is not None
        assert landed.on_hand_quantity == Decimal("4.0000")

    async def test_receiving_more_of_it_is_still_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The gate is a gate, not a removal. Nothing new may be *started* into
        stock without its lot - which is the whole point of the flag."""
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_batch_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        with pytest.raises(BusinessRuleError) as excinfo:
            async with transaction(session):
                await inv.post_movement(
                    session,
                    tenant,
                    item_id=bench.item_id,
                    warehouse_id=bench.destination,
                    quantity=Decimal("5"),
                    transaction_type=TransactionType.PURCHASE_RECEIPT,
                    reference_type="goods_receipt",
                    reference_id=None,
                    reference_code=_code("GRN"),
                    reason="a fresh delivery",
                    unit_cost=Decimal("40"),
                )
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED

    async def test_the_untraced_arrival_is_recorded_on_its_own_movement(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The exemption is right and it is not free: goods went into a tracked
        item's position carrying no lot. Nothing else in the system would ever
        say so, and a recall is not the moment to find out - so the movement's
        audit row records the gap, and an operator can list it."""
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_batch_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        gaps = (
            await session.execute(
                text(
                    "SELECT context->>'traceability_gap' AS gap FROM audit_logs "
                    "WHERE company_id = :company_id AND entity_type = 'inventory_transaction' "
                    "AND context ? 'traceability_gap'"
                ),
                {"company_id": tenant.company_id},
            )
        ).all()
        assert [row.gap for row in gaps] == ['["batch"]']

    async def test_an_ordinary_arrival_records_no_gap(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Absent on every normal movement, so a search for it returns exactly
        the quantities somebody has to reconcile by hand."""
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        flagged = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE company_id = :company_id "
                    "AND context ? 'traceability_gap'"
                ),
                {"company_id": tenant.company_id},
            )
        ).scalar_one()
        assert flagged == 0


class TestTheSerialTwinOfIt:
    """The same shape, one flag over - and it does NOT get the same answer.

    The batch rule may be left open outbound because ``batch_id`` is part of
    ``BalanceGrain``: a batch-less issue can only ever reach the batch-less
    position, which is exactly the stock that predates the flag.

    ``serial_id`` is not part of the grain, so a serial-less issue draws on the
    whole position - identified units included - and picks which ones by nobody's
    decision. Ungating it lets stock leave while every ``stock_serials`` row
    still says IN_STOCK, and lets the same unit be issued twice. That is the
    defect the serial lifecycle exists to prevent, and it is live on the ordinary
    path of every serial-tracked item, not only after a flag flip.

    So the untraced quantity is refused until somebody identifies it. That is not
    stranding: the stock is recoverable by an ordinary operator action, proved
    below. The asymmetry with batch is deliberate, and
    ``test_the_batch_and_serial_rules_disagree_on_purpose`` pins it.
    """

    async def test_the_goods_arrive_with_no_serial_rows_at_all(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_serial_tracked")

        received = await inv.StockTransferService(session, tenant).receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

        units = (
            await session.execute(
                text("SELECT count(*) FROM stock_serials WHERE item_id = :item_id"),
                {"item_id": bench.item_id},
            )
        ).scalar_one()
        assert units == 0

    async def test_the_untraced_units_must_be_identified_before_they_can_leave(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Refused while anonymous, movable once identified - and never stranded.

        The remedy is an ordinary operator action rather than a migration or a
        support ticket, which is what makes refusing the anonymous issue the
        right trade: the alternative leaves a traceability hole open on every
        serial-tracked issue in the product, for good.
        """
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_serial_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        with pytest.raises(BusinessRuleError) as excinfo:
            await issue_out(session, tenant, bench)
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

        traceability = inv.TraceabilityService(session, tenant)
        registered = [
            await traceability.create_serial(
                StockSerialCreate(
                    item_id=bench.item_id,
                    serial_number=f"LEGACY-{bench.item_id.hex[:6].upper()}-{n}",
                    warehouse_id=bench.destination,
                )
            )
            for n in range(4)
        ]

        for unit in registered:
            await issue_out(session, tenant, bench, quantity="1", serial_id=unit.id)

        emptied = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert emptied is not None
        assert emptied.on_hand_quantity == Decimal("0.0000")

    async def test_receiving_more_of_it_is_still_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_serial_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        with pytest.raises(BusinessRuleError) as excinfo:
            async with transaction(session):
                await inv.post_movement(
                    session,
                    tenant,
                    item_id=bench.item_id,
                    warehouse_id=bench.destination,
                    quantity=Decimal("1"),
                    transaction_type=TransactionType.PURCHASE_RECEIPT,
                    reference_type="goods_receipt",
                    reference_id=None,
                    reference_code=_code("GRN"),
                    reason="a fresh delivery",
                    unit_cost=Decimal("40"),
                )
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    async def test_the_untraced_arrival_is_recorded(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        await start_tracking(session, bench.item_id, "is_serial_tracked")
        await inv.StockTransferService(session, tenant).receive(transfer_id)

        gaps = (
            await session.execute(
                text(
                    "SELECT context->>'traceability_gap' AS gap FROM audit_logs "
                    "WHERE company_id = :company_id "
                    "AND context ? 'traceability_gap'"
                ),
                {"company_id": tenant.company_id},
            )
        ).all()
        assert [row.gap for row in gaps] == ['["serial"]']


# -----------------------------------------------------------------------------
# The guard on the item master
# -----------------------------------------------------------------------------


class TestTheFlagCannotBeFlippedUnderLiveStock:
    """The prevention. ``update_item`` used to apply no stock guard at all."""

    async def test_batch_tracking_cannot_be_turned_on_over_stock_on_hand(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)

        with pytest.raises(BusinessRuleError) as excinfo:
            await products.update_item(
                session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=True)
            )
        assert excinfo.value.code is ErrorCode.REFERENCED_RECORD_IN_USE
        assert excinfo.value.details["fields"] == ["is_batch_tracked"]

    async def test_serial_tracking_cannot_be_either(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)

        with pytest.raises(BusinessRuleError) as excinfo:
            await products.update_item(
                session, tenant, bench.item_id, ItemUpdate(is_serial_tracked=True)
            )
        assert excinfo.value.details["fields"] == ["is_serial_tracked"]

    async def test_the_refusal_is_actionable(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """A message that names the item and says what to do about it - the
        thing the defect had none of."""
        bench = await seed_bench(session, tenant)

        with pytest.raises(BusinessRuleError) as excinfo:
            await products.update_item(
                session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=True)
            )
        assert "still holds stock" in excinfo.value.message
        assert "in transit" in excinfo.value.message
        assert excinfo.value.details["item_id"] == str(bench.item_id)

    async def test_stock_that_is_only_in_transit_still_counts(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The exact hole a guard reading ``on_hand_quantity`` alone would leave:
        the dispatched quantity is off the source's on-hand and standing in the
        destination's ``in_transit_quantity``, and its arrival cannot be refused.
        That is the sequence at the top of this file."""
        bench = await seed_bench(session, tenant, on_hand="4")
        await dispatched_transfer(session, tenant, bench, quantity="4")

        source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
        assert source is not None
        assert source.on_hand_quantity == Decimal("0.0000")

        with pytest.raises(BusinessRuleError, match="still holds stock"):
            await products.update_item(
                session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=True)
            )

    async def test_turning_tracking_off_is_refused_on_the_same_terms(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """It strands nothing, but it splits a live item across two grain shapes.
        One sentence - how stock is identified changes only while there is no
        stock - is worth more than the exception."""
        bench = await seed_bench(session, tenant)
        await start_tracking(session, bench.item_id, "is_batch_tracked")

        with pytest.raises(BusinessRuleError, match="still holds stock"):
            await products.update_item(
                session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=False)
            )

    async def test_an_item_holding_nothing_may_still_start_being_tracked(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The guard is a guard, not a wall. "We are starting to lot-track this
        part" stays a decision the business can take."""
        bench = await seed_bench(session, tenant, on_hand="0")

        updated = await products.update_item(
            session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=True)
        )
        assert updated.is_batch_tracked is True

    async def test_and_once_the_balance_is_emptied_again(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """A zeroed balance row is still a row. It must not keep the flag locked
        for good, or the guard becomes the wall."""
        bench = await seed_bench(session, tenant, on_hand="6")
        async with transaction(session):
            await inv.post_movement(
                session,
                tenant,
                item_id=bench.item_id,
                warehouse_id=bench.source,
                quantity=Decimal("6"),
                transaction_type=TransactionType.MATERIAL_ISSUE,
                reference_type="material_issue",
                reference_id=None,
                reference_code=_code("MI"),
                reason="clearing the position",
            )

        updated = await products.update_item(
            session, tenant, bench.item_id, ItemUpdate(is_batch_tracked=True)
        )
        assert updated.is_batch_tracked is True

    async def test_a_flag_restated_at_its_current_value_is_not_a_change(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """A form that posts every field must not be refused for the fields it
        did not touch."""
        bench = await seed_bench(session, tenant)

        updated = await products.update_item(
            session,
            tenant,
            bench.item_id,
            ItemUpdate(is_batch_tracked=False, is_serial_tracked=False, name="Brass rod, 12mm"),
        )
        assert updated.name == "Brass rod, 12mm"

    async def test_every_other_edit_is_untouched(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)

        updated = await products.update_item(
            session, tenant, bench.item_id, ItemUpdate(name="Brass rod, 16mm")
        )
        assert updated.name == "Brass rod, 16mm"

    async def test_the_stock_probe_is_tenant_scoped(
        self, session: AsyncSession, tenant: TenantContext, other_company
    ) -> None:
        """The probe is raw SQL against ``inventory_balances``, so its tenant
        predicate is hand-written and worth asserting rather than reading. A
        balance row of this company's must not answer for another company's
        repository - nor the other way round."""
        bench = await seed_bench(session, tenant)
        repo = ItemRepository(session, tenant)
        assert await repo.has_stock_balance(bench.item_id) is True

        other = TenantContext(company_id=other_company.id, user_id=tenant.user_id)
        assert await ItemRepository(session, other).has_stock_balance(bench.item_id) is False


# -----------------------------------------------------------------------------
# The document that could raise the situation
# -----------------------------------------------------------------------------


class TestATransferNamesTheTraceabilityItNeeds:
    """The engine gates traceability on arrivals, and the outbound leg of a
    transfer is not one - so the *document* answers for its own lines, at the
    moment they are keyed, where nothing has moved and the operator can fix it.
    """

    async def test_a_batch_tracked_item_must_name_its_batch(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant, on_hand="0")
        await start_tracking(session, bench.item_id, "is_batch_tracked")

        with pytest.raises(BusinessRuleError) as excinfo:
            await inv.StockTransferService(session, tenant).create(
                StockTransferCreate(
                    from_warehouse_id=bench.source,
                    to_warehouse_id=bench.destination,
                    reason="move the lot",
                    lines=[StockTransferLineCreate(item_id=bench.item_id, quantity=Decimal("1"))],
                )
            )
        assert excinfo.value.code is ErrorCode.BATCH_REQUIRED
        assert excinfo.value.details["line_number"] == 1

    async def test_a_serial_tracked_item_cannot_be_transferred_at_all(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """``stock_transfer_lines`` carries a batch and no unit list, so there is
        nowhere to name what is being shipped - the same "no contract on this
        path" refusal assembly and job work already make."""
        bench = await seed_bench(session, tenant, on_hand="0")
        await start_tracking(session, bench.item_id, "is_serial_tracked")

        with pytest.raises(BusinessRuleError) as excinfo:
            await inv.StockTransferService(session, tenant).create(
                StockTransferCreate(
                    from_warehouse_id=bench.source,
                    to_warehouse_id=bench.destination,
                    reason="move the units",
                    lines=[StockTransferLineCreate(item_id=bench.item_id, quantity=Decimal("1"))],
                )
            )
        assert excinfo.value.code is ErrorCode.SERIAL_REQUIRED

    async def test_an_untracked_item_is_unaffected(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await dispatched_transfer(session, tenant, bench)
        received = await inv.StockTransferService(session, tenant).receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED
