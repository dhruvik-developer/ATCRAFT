"""A stock transfer must never be able to end up somewhere it cannot leave.

``TRANSFER_FLOW`` has exactly one edge out of ``IN_TRANSIT``: ``RECEIVED``. The
cancel edges are from DRAFT and APPROVED only, because stock on a vehicle cannot
be un-dispatched. So a transfer that dispatches and then cannot be received is
stranded for good, and the stock with it - off the source's on-hand, sitting in
the destination's ``in_transit_quantity``, reachable by no document at all.

An earlier round closed the version of this that came from a *bad bin keyed at
create* (``test_inventory_transfer_positions.py``). That was only half of it.
Master data changes between create, dispatch and receive, and reproduced against
PostgreSQL:

* deactivate the item after the transfer is approved and ``dispatch`` posted the
  outbound leg happily - the destination grain was checked for an inactive
  warehouse and an inactive bin but never for an inactive item - and then
  ``receive`` refused the inbound leg, which *is* a real movement and *is* held
  to the item's active flag. Four units gone from the source, unreachable.

Validating harder at dispatch does not fix this on its own: the tighter dispatch
is, the more often stock is refused; the looser it is, the more often receive
strands it. What must hold is an *invariant*:

    anything ``dispatch`` lets through, ``receive`` accepts.

Two halves hold it up, and both are asserted here:

1. **Dispatch decides.** It validates the destination with the same rules
   ``receive`` will apply to it, and refusing there costs nothing: nothing has
   moved and the document stays APPROVED, which is cancellable.
2. **Receive completes.** Master data can change while the goods travel and no
   check at dispatch can see the future, so the arrival is held to the rules
   about the position being real - and not to policy that changed after the
   goods left.

The last class is the invariant itself, swept over every way the destination can
go bad, in both windows.

Isolation uses the shared ``session`` fixture from ``tests/conftest.py``:
nothing here needs a second tenant or a schema of its own, and every module that
builds a private one costs the server a set of locks it has to hold through
teardown.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import transaction
from src.core.deps import TenantContext
from src.core.errors import AppError, BusinessRuleError
from src.modules.inventory import service as inv
from src.modules.inventory.models import (
    StockTransfer,
    TransactionType,
    TransferStatus,
)
from src.modules.inventory.schemas import StockTransferCreate, StockTransferLineCreate
from src.modules.products.models import Item
from src.modules.warehouses.models import StorageLocation, StorageLocationType, Warehouse
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


# -----------------------------------------------------------------------------
# The bench: one item, two warehouses, ten on hand at the source
# -----------------------------------------------------------------------------


class Bench:
    def __init__(
        self,
        item_id: uuid.UUID,
        source: uuid.UUID,
        destination: uuid.UUID,
        destination_bin: uuid.UUID | None,
    ) -> None:
        self.item_id = item_id
        self.source = source
        self.destination = destination
        self.destination_bin = destination_bin


async def seed_bench(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    on_hand: str = "10",
    with_bin: bool = False,
) -> Bench:
    async with seeded(session):
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=_code("RM"),
            gst_rate=Decimal("18"),
            standard_cost=Decimal("40"),
        )
        source = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WA")
        )
        destination = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=_code("WB")
        )
        destination_bin_id: uuid.UUID | None = None
        if with_bin:
            location = StorageLocation(
                company_id=tenant.company_id,
                warehouse_id=destination.id,
                code=_code("BIN"),
                name="Inbound bay",
                location_type=StorageLocationType.BIN,
            )
            session.add(location)
            await session.flush()
            destination_bin_id = location.id
    bench = Bench(item.id, source.id, destination.id, destination_bin_id)
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
            reason="stranding bench",
            unit_cost=Decimal("40"),
        )
    return bench


def transfer_payload(bench: Bench, *, quantity: str = "4") -> StockTransferCreate:
    return StockTransferCreate(
        from_warehouse_id=bench.source,
        to_warehouse_id=bench.destination,
        reason="move stock between the stores",
        lines=[
            StockTransferLineCreate(
                item_id=bench.item_id,
                quantity=Decimal(quantity),
                to_location_id=bench.destination_bin,
            )
        ],
    )


async def approved_transfer(
    session: AsyncSession, tenant: TenantContext, bench: Bench
) -> uuid.UUID:
    service = inv.StockTransferService(session, tenant)
    transfer = await service.create(transfer_payload(bench))
    transfer_id = transfer.id
    await service.approve(transfer_id)
    return transfer_id


# --- the master-data edits a document cannot see coming -----------------------


async def deactivate_item(session: AsyncSession, item_id: uuid.UUID) -> None:
    """An ordinary edit. ``products`` applies no stock guard to the active flag -
    a deactivated item may still give its stock up, which is the whole point of
    the asymmetry - and the lifecycle machine allows ACTIVE -> INACTIVE at any
    time."""
    async with seeded(session):
        item = (await session.execute(select(Item).where(Item.id == item_id))).scalar_one()
        item.is_active = False


async def deactivate_warehouse(session: AsyncSession, warehouse_id: uuid.UUID) -> None:
    async with seeded(session):
        warehouse = (
            await session.execute(select(Warehouse).where(Warehouse.id == warehouse_id))
        ).scalar_one()
        warehouse.is_active = False


async def deactivate_bin(session: AsyncSession, location_id: uuid.UUID) -> None:
    async with seeded(session):
        location = (
            await session.execute(select(StorageLocation).where(StorageLocation.id == location_id))
        ).scalar_one()
        location.is_active = False


async def start_batch_tracking(session: AsyncSession, item_id: uuid.UUID) -> None:
    """Written through the ORM, past ``products.update_item``.

    That service now refuses a tracking flip while the item holds stock -
    including stock standing in ``in_transit_quantity``, which is exactly this
    situation (``tests/integration/test_inventory_tracking_flags.py``). The
    column is written directly here on purpose: this file is about what the
    *engine* does when it meets such a row, and a database restored from before
    that guard existed can contain one however carefully the API behaves now.
    """
    async with seeded(session):
        item = (await session.execute(select(Item).where(Item.id == item_id))).scalar_one()
        item.is_batch_tracked = True


async def start_serial_tracking(session: AsyncSession, item_id: uuid.UUID) -> None:
    """See :func:`start_batch_tracking` - same bypass, same reason."""
    async with seeded(session):
        item = (await session.execute(select(Item).where(Item.id == item_id))).scalar_one()
        item.is_serial_tracked = True


#: Every way the destination of an in-flight transfer can go bad, as
#: ``(name, needs a bin, edit)``. Swept over both windows below.
DESTINATION_DRIFT: list[tuple[str, bool, Any]] = [
    ("item deactivated", False, lambda s, b: deactivate_item(s, b.item_id)),
    (
        "destination warehouse deactivated",
        False,
        lambda s, b: deactivate_warehouse(s, b.destination),
    ),
    ("destination bin deactivated", True, lambda s, b: deactivate_bin(s, b.destination_bin)),
    ("batch tracking switched on", False, lambda s, b: start_batch_tracking(s, b.item_id)),
    ("serial tracking switched on", False, lambda s, b: start_serial_tracking(s, b.item_id)),
]

DRIFT_IDS = [name for name, _, _ in DESTINATION_DRIFT]


# -----------------------------------------------------------------------------
# The reported reproduction
# -----------------------------------------------------------------------------


class TestAnItemDeactivatedBeforeDispatch:
    """The exact sequence reproduced against PostgreSQL.

    Seed item I active, warehouses W1/W2, 10 on hand at W1. Create a 4-unit line
    W1 -> W2, approve. Deactivate I - an ordinary edit, no stock guard. Dispatch
    used to succeed, because the destination *grain* was checked for an inactive
    warehouse and an inactive bin but never for an inactive item, while
    ``validate_movement`` - which ``receive`` runs - checks all three.
    """

    async def test_dispatch_refuses_an_item_deactivated_after_approval(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await deactivate_item(session, bench.item_id)

        with pytest.raises(BusinessRuleError, match="inactive"):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

    async def test_and_the_stock_is_exactly_where_it_was(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Refusing at dispatch costs nothing, which is why dispatch is the
        right place to refuse."""
        bench = await seed_bench(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await deactivate_item(session, bench.item_id)

        with pytest.raises(BusinessRuleError):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
        assert source is not None
        assert source.on_hand_quantity == Decimal("10.0000")
        assert await inv.get_balance(session, tenant, bench.item_id, bench.destination) is None
        posted = (
            await session.execute(
                text("SELECT count(*) FROM inventory_transactions WHERE reference_id = :ref"),
                {"ref": transfer_id},
            )
        ).scalar_one()
        assert posted == 0

    async def test_the_document_is_still_approved_and_can_be_cancelled(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Not stranded: APPROVED has a cancel edge, IN_TRANSIT does not. That
        difference is the whole reason the refusal has to land at dispatch."""
        bench = await seed_bench(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await deactivate_item(session, bench.item_id)

        with pytest.raises(BusinessRuleError):
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)

        service = inv.StockTransferService(session, tenant)
        assert (await service.get_transfer(transfer_id)).status is TransferStatus.APPROVED
        cancelled = await service.cancel(transfer_id, reason="item withdrawn from the range")
        assert cancelled.status is TransferStatus.CANCELLED

    async def test_the_refusal_names_the_item(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await deactivate_item(session, bench.item_id)

        with pytest.raises(BusinessRuleError) as excinfo:
            await inv.StockTransferService(session, tenant).dispatch(transfer_id)
        assert "item_code" in excinfo.value.details

    async def test_an_active_item_still_dispatches_and_receives(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The guard is a guard, not a wall."""
        bench = await seed_bench(session, tenant)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)

        await service.dispatch(transfer_id)
        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

        landed = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert landed is not None
        assert landed.on_hand_quantity == Decimal("4.0000")
        assert landed.in_transit_quantity == Decimal("0.0000")


class TestTheImmediateTransferChecksTheSameThing:
    """``transfer_stock`` locks its destination before it posts, so the grain
    check is the only thing standing between an inactive item and a position
    created for it. Both legs are in one transaction here, so nothing can be
    stranded - but the answer has to be the same one."""

    async def test_an_inactive_item_is_refused(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        bench = await seed_bench(session, tenant)
        await deactivate_item(session, bench.item_id)

        with pytest.raises(BusinessRuleError, match="inactive"):
            await inv.transfer_stock(
                session,
                tenant,
                item_id=bench.item_id,
                from_warehouse_id=bench.source,
                to_warehouse_id=bench.destination,
                quantity=Decimal("2"),
                reference_code="ST/DIRECT",
                reason="straight across",
            )

        source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
        assert source is not None
        assert source.on_hand_quantity == Decimal("10.0000")


# -----------------------------------------------------------------------------
# Master data that changes while the lorry is on the road
# -----------------------------------------------------------------------------


class TestDriftAfterDispatchDoesNotStrandTheStock:
    """Dispatch cannot see the future, so receive must not re-apply policy.

    Every case here dispatches a perfectly good transfer and *then* breaks the
    destination. The goods are already off the source's shelf; the only question
    left is whether the company can book them in.
    """

    @pytest.mark.parametrize(
        ("needs_bin", "edit"),
        [(needs_bin, edit) for _, needs_bin, edit in DESTINATION_DRIFT],
        ids=DRIFT_IDS,
    )
    async def test_the_arrival_is_still_booked_in(
        self, session: AsyncSession, tenant: TenantContext, needs_bin, edit
    ) -> None:
        bench = await seed_bench(session, tenant, with_bin=needs_bin)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)

        await edit(session, bench)

        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

        landed = await inv.get_balance(
            session,
            tenant,
            bench.item_id,
            bench.destination,
            location_id=bench.destination_bin,
        )
        assert landed is not None
        assert landed.on_hand_quantity == Decimal("4.0000")
        # And nothing is left hanging in transit at a position nobody can reach.
        assert landed.in_transit_quantity == Decimal("0.0000")

    async def test_the_stock_carries_its_value_across(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The exemption is about policy, not about accounting: the inbound leg
        is still booked at the cost the outbound leg took it out with."""
        bench = await seed_bench(session, tenant)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)
        await deactivate_item(session, bench.item_id)
        await service.receive(transfer_id)

        landed = await inv.get_balance(session, tenant, bench.item_id, bench.destination)
        assert landed is not None
        assert landed.average_cost == Decimal("40.000000")

    async def test_nothing_of_the_company_is_lost_on_the_way(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """Source plus destination, before and after: the company owns the same
        ten units throughout."""
        bench = await seed_bench(session, tenant)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)
        await deactivate_item(session, bench.item_id)
        await service.receive(transfer_id)

        owned = Decimal("0")
        for warehouse_id in (bench.source, bench.destination):
            balance = await inv.get_balance(session, tenant, bench.item_id, warehouse_id)
            assert balance is not None
            owned += Decimal(balance.on_hand_quantity) + Decimal(balance.in_transit_quantity)
        assert owned == Decimal("10.0000")


# -----------------------------------------------------------------------------
# The invariant itself
# -----------------------------------------------------------------------------


class TestWhateverDispatchAcceptsReceiveAccepts:
    """The property, swept over every way the destination can go bad.

    Not "dispatch refuses X" and not "receive accepts Y" - those are the two
    halves, and either one alone is a rule someone can tighten in isolation and
    reopen the hole. The property is the relation between them: for every
    destination state, if ``dispatch`` returned then ``receive`` must return
    too. A test written this way fails whichever side drifts.
    """

    @pytest.mark.parametrize(
        ("needs_bin", "edit"),
        [(needs_bin, edit) for _, needs_bin, edit in DESTINATION_DRIFT],
        ids=DRIFT_IDS,
    )
    async def test_the_pair_never_disagrees_when_the_edit_lands_before_dispatch(
        self, session: AsyncSession, tenant: TenantContext, needs_bin, edit
    ) -> None:
        bench = await seed_bench(session, tenant, with_bin=needs_bin)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)

        await edit(session, bench)

        try:
            await service.dispatch(transfer_id)
        except AppError:
            # Refused before anything moved. The document is still APPROVED, so
            # it can be cancelled - nothing is stranded.
            assert (await service.get_transfer(transfer_id)).status is TransferStatus.APPROVED
            source = await inv.get_balance(session, tenant, bench.item_id, bench.source)
            assert source is not None
            assert source.on_hand_quantity == Decimal("10.0000")
            return

        # Dispatch let it through, so receive has to take it. Anything else
        # leaves the stock in transit with no edge out of IN_TRANSIT.
        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

    @pytest.mark.parametrize(
        ("needs_bin", "edit"),
        [(needs_bin, edit) for _, needs_bin, edit in DESTINATION_DRIFT],
        ids=DRIFT_IDS,
    )
    async def test_and_never_when_it_lands_after_dispatch(
        self, session: AsyncSession, tenant: TenantContext, needs_bin, edit
    ) -> None:
        """The window no validation can close. Dispatch has already committed,
        so receive has no right to refuse."""
        bench = await seed_bench(session, tenant, with_bin=needs_bin)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)

        await edit(session, bench)

        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED

    async def test_no_transfer_is_left_in_transit_by_any_of_it(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The end state that matters, over every drift at once: run one
        transfer per case and afterwards this tenant holds no IN_TRANSIT
        document. A stranded one would still be sitting here."""
        service = inv.StockTransferService(session, tenant)
        for _, needs_bin, edit in DESTINATION_DRIFT:
            # A bench per case: the edits are one-way, and a second transfer of
            # an item already deactivated would be refused at dispatch - which
            # is correct behaviour, and not what this case is measuring.
            bench = await seed_bench(session, tenant, with_bin=needs_bin)
            transfer_id = await approved_transfer(session, tenant, bench)
            await service.dispatch(transfer_id)
            await edit(session, bench)
            await service.receive(transfer_id)

        stuck = (
            (
                await session.execute(
                    select(StockTransfer).where(
                        StockTransfer.company_id == tenant.company_id,
                        StockTransfer.status == TransferStatus.IN_TRANSIT,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert stuck == []


class TestTheOneDriftThatWouldStillStrandIsPreventedUpstream:
    """Soft deletion is the gap the exemption deliberately does not cover.

    ``location_facts`` and ``warehouse_facts`` exclude soft-deleted rows, so a
    destination retired mid-flight would come back as "not found" and
    ``receive`` would refuse - the one master-data change that could still
    strand a lorry-load. It cannot happen through the services, and this is why:
    ``warehouses`` refuses to retire a warehouse or a bin whose balance row is
    non-zero, and ``in_transit_quantity`` is one of the columns it counts.

    Asserted here rather than assumed, because the whole argument for letting
    the arrival through on the *policy* rules rests on the *integrity* rules
    being unable to break underneath it. If that guard is ever narrowed, this
    fails and the exemption has to be revisited - not silently widened.
    """

    async def test_a_destination_warehouse_cannot_be_retired_while_stock_is_in_transit(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        from src.modules.warehouses.service import WarehouseService

        bench = await seed_bench(session, tenant)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)

        with pytest.raises(BusinessRuleError, match="still holds stock"):
            await WarehouseService(session, tenant).delete_warehouse(
                bench.destination, reason="closing the store"
            )

    async def test_a_destination_bin_cannot_be_retired_while_stock_is_in_transit(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        from src.modules.warehouses.service import StorageLocationService

        bench = await seed_bench(session, tenant, with_bin=True)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)
        assert bench.destination_bin is not None

        with pytest.raises(BusinessRuleError, match="still holds stock"):
            await StorageLocationService(session, tenant).delete_location(
                bench.destination_bin, reason="re-racking the bay"
            )

    async def test_and_the_transfer_can_still_be_received_afterwards(
        self, session: AsyncSession, tenant: TenantContext
    ) -> None:
        """The guard refused; nothing about the transfer changed."""
        from src.modules.warehouses.service import WarehouseService

        bench = await seed_bench(session, tenant)
        service = inv.StockTransferService(session, tenant)
        transfer_id = await approved_transfer(session, tenant, bench)
        await service.dispatch(transfer_id)

        with pytest.raises(BusinessRuleError):
            await WarehouseService(session, tenant).delete_warehouse(
                bench.destination, reason="closing the store"
            )

        received = await service.receive(transfer_id)
        assert received.status is TransferStatus.RECEIVED
