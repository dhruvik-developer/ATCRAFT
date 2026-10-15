"""Bin moves, scrap disposals, internal issues - and the transfer and adjustment
lines that now carry a batch, a bin and a cost - against a real database.

What is proved, document by document:

* a **bin move** conserves quantity and value exactly, layer by layer, and is
  refused before anything moves when a line could never post;
* **scrap** is visible on the stock summary, and a **scrap disposal** draws the
  scrap pool - oldest scrapped cost first - without touching on-hand or its
  value; scrap that predates its cost queue is reconciled, not stranded;
* an **internal issue** posts below the approval value, needs a second person
  above it, and is refused - with nothing posted - when the cost it really
  consumes turns out to be over the line;
* the **scrap register** and **consumption by department** reports add up to
  the documents behind them;
* every list endpoint lists a row the test itself created.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from src.core.clock import today
from src.core.deps import TenantContext
from src.core.errors import (
    AuthorizationError,
    BusinessRuleError,
    ErrorCode,
    InsufficientStockError,
    InvalidStateTransitionError,
    NotFoundError,
    ValidationError,
)
from src.core.pagination import PageParams, SortParams
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.inventory.models import (
    BinMove,
    CostLayerPool,
    InternalIssueStatus,
    InventoryTransaction,
    ScrapDisposal,
    StockBatch,
    StockState,
    TransactionType,
)
from src.modules.inventory.repository import CostLayerRepository
from src.modules.inventory.schemas import (
    BalanceFilters,
    BinMoveCreate,
    BinMoveFilters,
    InternalIssueCreate,
    InternalIssueFilters,
    ScrapDisposalCreate,
    ScrapDisposalFilters,
    StockAdjustmentCreate,
    StockAdjustmentUpdate,
    StockTransferCreate,
)
from src.modules.inventory.service import (
    BinMoveService,
    InternalIssueService,
    InventoryQueryService,
    ScrapDisposalService,
    StockAdjustmentService,
    StockTransferService,
    change_stock_state,
    get_balance,
    post_movement,
)
from src.modules.organization.models import CostCentre, CostCentreType, Department
from src.modules.products.schemas import ItemUpdate
from src.modules.products.service import update_item
from src.modules.reports.repository import ReportQueryRepository
from src.modules.reports.schemas import ReportFilters, ReportPeriod
from src.modules.warehouses.models import StorageLocation, StorageLocationType
from src.modules.warehouses.service import StorageLocationService, WarehouseService
from tests.seed_support import seed_item, seed_user, seed_warehouse

pytestmark = pytest.mark.integration


async def test_default_bin_assignment_and_retirement_share_the_same_contract(
    session,
    tenant,
    unique_code,
    other_company,
):
    item = await seed_item(
        session, company_id=tenant.company_id, code=unique_code("DB"), can_purchase=True
    )
    warehouse = await seed_warehouse(session, company_id=tenant.company_id, code=unique_code("DW"))
    other_warehouse = await seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("DX")
    )
    location = await _bin(session, tenant.company_id, warehouse, unique_code)
    foreign_warehouse = await seed_warehouse(
        session, company_id=other_company.id, code=unique_code("FW")
    )
    foreign_bin = await _bin(session, other_company.id, foreign_warehouse, unique_code)
    item_id, warehouse_id, location_id, foreign_id, other_id = (
        item.id,
        warehouse.id,
        location.id,
        foreign_bin.id,
        other_warehouse.id,
    )
    with pytest.raises(NotFoundError):
        await update_item(
            session,
            tenant,
            item_id,
            ItemUpdate(default_warehouse_id=warehouse_id, default_location_id=foreign_id),
        )
    with pytest.raises(BusinessRuleError, match="default warehouse"):
        await update_item(
            session,
            tenant,
            item_id,
            ItemUpdate(default_warehouse_id=other_id, default_location_id=location_id),
        )
    await update_item(
        session,
        tenant,
        item_id,
        ItemUpdate(default_warehouse_id=warehouse_id, default_location_id=location_id),
    )
    service = StorageLocationService(session, tenant)
    with pytest.raises(BusinessRuleError, match="default bin"):
        await service.set_active(location_id, active=False, reason="Retire the old rack")
    with pytest.raises(BusinessRuleError, match="default bin"):
        await service.delete_location(location_id, reason="Retire the old rack")
    with pytest.raises(BusinessRuleError, match="default bins"):
        await WarehouseService(session, tenant).delete_warehouse(
            warehouse_id, reason="Retire the old store"
        )
    await update_item(session, tenant, item_id, ItemUpdate(default_location_id=None))
    await service.set_active(location_id, active=False, reason="Retire the old rack")
    with pytest.raises(BusinessRuleError, match="inactive"):
        await update_item(session, tenant, item_id, ItemUpdate(default_location_id=location_id))


PAGE = PageParams()
UNSORTED = SortParams()


async def _receive(
    session,
    tenant,
    item,
    warehouse,
    quantity: str,
    cost: str,
    *,
    location_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> None:
    await post_movement(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        location_id=location_id,
        batch_id=batch_id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="test",
        reference_id=None,
        reference_code=None,
        reason="Opening test stock",
        unit_cost=Decimal(cost),
    )


async def _stock(session, tenant, unique_code, *, cost: str = "12", **item_fields):
    """An item with 20 units at ``cost`` at the unbinned position of a new warehouse."""
    item = await seed_item(
        session,
        company_id=tenant.company_id,
        code=unique_code("IT"),
        standard_cost=Decimal(cost),
        **item_fields,
    )
    warehouse = await seed_warehouse(session, company_id=tenant.company_id, code=unique_code("WH"))
    await _receive(session, tenant, item, warehouse, "20", cost)
    return item, warehouse


async def _bin(session, company_id: uuid.UUID, warehouse, unique_code, **fields) -> StorageLocation:
    row = StorageLocation(
        company_id=company_id,
        warehouse_id=warehouse.id,
        code=unique_code("BN"),
        name="Test bin",
        location_type=StorageLocationType.BIN,
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


async def _department(session, tenant, unique_code, **fields) -> Department:
    row = Department(company_id=tenant.company_id, code=unique_code("DP"), name="Maint", **fields)
    session.add(row)
    await session.flush()
    return row


async def _cost_centre(session, company_id: uuid.UUID, unique_code) -> CostCentre:
    row = CostCentre(
        company_id=company_id,
        code=unique_code("CC"),
        name="Maintenance",
        cost_centre_type=CostCentreType.SERVICE,
    )
    session.add(row)
    await session.flush()
    return row


async def _ledger(session, reference_id: uuid.UUID) -> list[InventoryTransaction]:
    stmt = (
        select(InventoryTransaction)
        .where(InventoryTransaction.reference_id == reference_id)
        .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def _period() -> ReportPeriod:
    return ReportPeriod(date_from=today(), date_to=today())


# =============================================================================
# Bin moves
# =============================================================================


class TestBinMoves:
    async def test_a_putaway_conserves_quantity_and_value_and_is_listed(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)

        move = await BinMoveService(session, tenant).create(
            BinMoveCreate.model_validate(
                {
                    "warehouse_id": str(warehouse.id),
                    "reason": "Put received goods away",
                    "lines": [
                        {"item_id": str(item.id), "quantity": "7", "to_location_id": str(rack.id)}
                    ],
                }
            )
        )

        assert move.status == "POSTED" and move.posted_at is not None
        assert move.total_value == Decimal("84.00")
        assert move.lines[0].unit_cost == Decimal("12") and move.lines[0].item_code == item.code
        source = await get_balance(session, tenant, item.id, warehouse.id)
        destination = await get_balance(session, tenant, item.id, warehouse.id, location_id=rack.id)
        assert source is not None and destination is not None
        assert (source.on_hand_quantity, destination.on_hand_quantity) == (13, 7)
        assert source.stock_value + destination.stock_value == Decimal("240")

        legs = await _ledger(session, move.id)
        assert [leg.transaction_type for leg in legs] == [
            TransactionType.TRANSFER_OUT,
            TransactionType.TRANSFER_IN,
        ]
        assert {leg.reference_type for leg in legs} == {"bin_move"}

        rows, _ = await BinMoveService(session, tenant).list_moves(BinMoveFilters(), PAGE, UNSORTED)
        assert move.id in {row.id for row in rows}
        balances, _ = await InventoryQueryService(session, tenant).list_balances(
            BalanceFilters(item_id=item.id, location_id=rack.id), PAGE, UNSORTED
        )
        assert [row.location_code for row in balances] == [rack.code]

    async def test_each_cost_layer_moves_with_its_own_price(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="100")
        await _receive(session, tenant, item, warehouse, "10", "120")
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)

        move = await BinMoveService(session, tenant).create(
            BinMoveCreate.model_validate(
                {
                    "warehouse_id": str(warehouse.id),
                    "reason": "Re-slot the older lot",
                    "lines": [
                        {"item_id": str(item.id), "quantity": "25", "to_location_id": str(rack.id)}
                    ],
                }
            )
        )

        destination = await get_balance(session, tenant, item.id, warehouse.id, location_id=rack.id)
        assert destination is not None
        layers = await CostLayerRepository(session, tenant).open_layers_for_balance(destination.id)
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in layers] == [
            (Decimal("20"), Decimal("100")),
            (Decimal("5"), Decimal("120")),
        ]
        assert move.total_value == Decimal("2600.00")

    async def test_a_bin_of_another_warehouse_is_refused_before_anything_moves(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        elsewhere = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WX")
        )
        stray = await _bin(session, tenant.company_id, elsewhere, unique_code)
        item_id, warehouse_id = item.id, warehouse.id

        with pytest.raises(BusinessRuleError, match="is not in warehouse"):
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse_id),
                        "reason": "Wrong store",
                        "lines": [
                            {
                                "item_id": str(item_id),
                                "quantity": "1",
                                "to_location_id": str(stray.id),
                            }
                        ],
                    }
                )
            )
        source = await get_balance(session, tenant, item_id, warehouse_id)
        assert source is not None and source.on_hand_quantity == 20

    async def test_another_tenants_bin_is_not_found(
        self, session, tenant, unique_code, other_company
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        foreign_store = await seed_warehouse(
            session, company_id=other_company.id, code=unique_code("OW")
        )
        foreign_bin = await _bin(session, other_company.id, foreign_store, unique_code)

        with pytest.raises(NotFoundError):
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse.id),
                        "reason": "Another company's bin",
                        "lines": [
                            {
                                "item_id": str(item.id),
                                "quantity": "1",
                                "to_location_id": str(foreign_bin.id),
                            }
                        ],
                    }
                )
            )

    async def test_a_retired_destination_bin_is_refused(self, session, tenant, unique_code) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        retired = await _bin(session, tenant.company_id, warehouse, unique_code, is_active=False)

        with pytest.raises(BusinessRuleError, match="cannot receive stock"):
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse.id),
                        "reason": "Into a retired bin",
                        "lines": [
                            {
                                "item_id": str(item.id),
                                "quantity": "1",
                                "to_location_id": str(retired.id),
                            }
                        ],
                    }
                )
            )

    async def test_a_batch_tracked_item_must_name_its_batch(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session, company_id=tenant.company_id, code=unique_code("BT"), is_batch_tracked=True
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WB")
        )
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)

        with pytest.raises(BusinessRuleError) as refused:
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse.id),
                        "reason": "Lot not named",
                        "lines": [
                            {
                                "item_id": str(item.id),
                                "quantity": "1",
                                "to_location_id": str(rack.id),
                            }
                        ],
                    }
                )
            )
        assert refused.value.code is ErrorCode.BATCH_REQUIRED

    async def test_a_serial_tracked_item_is_refused(self, session, tenant, unique_code) -> None:
        item = await seed_item(
            session, company_id=tenant.company_id, code=unique_code("SR"), is_serial_tracked=True
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WS")
        )
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)

        with pytest.raises(BusinessRuleError) as refused:
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse.id),
                        "reason": "Units not named",
                        "lines": [
                            {
                                "item_id": str(item.id),
                                "quantity": "1",
                                "to_location_id": str(rack.id),
                            }
                        ],
                    }
                )
            )
        assert refused.value.code is ErrorCode.SERIAL_REQUIRED

    async def test_moving_more_than_the_bin_holds_posts_nothing(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        item_id, warehouse_id, rack_id = item.id, warehouse.id, rack.id

        with pytest.raises(InsufficientStockError):
            await BinMoveService(session, tenant).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse_id),
                        "reason": "Too much",
                        "lines": [
                            {
                                "item_id": str(item_id),
                                "quantity": "21",
                                "to_location_id": str(rack_id),
                            }
                        ],
                    }
                )
            )
        moves = await session.execute(
            select(func.count()).select_from(BinMove).where(BinMove.warehouse_id == warehouse_id)
        )
        assert moves.scalar_one() == 0
        destination = await get_balance(session, tenant, item_id, warehouse_id, location_id=rack_id)
        assert destination is None or destination.on_hand_quantity == 0

    async def test_a_warehouse_restricted_principal_moves_only_its_own_stock(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        elsewhere = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WR")
        )
        restricted = replace(tenant, allowed_warehouse_ids=(elsewhere.id,))

        with pytest.raises(AuthorizationError):
            await BinMoveService(session, restricted).create(
                BinMoveCreate.model_validate(
                    {
                        "warehouse_id": str(warehouse.id),
                        "reason": "Not my store",
                        "lines": [
                            {
                                "item_id": str(item.id),
                                "quantity": "1",
                                "to_location_id": str(rack.id),
                            }
                        ],
                    }
                )
            )


# =============================================================================
# Scrap
# =============================================================================


async def _scrap(session, tenant, item, warehouse, quantity: str) -> None:
    await change_stock_state(
        session,
        tenant,
        item_id=item.id,
        warehouse_id=warehouse.id,
        quantity=Decimal(quantity),
        from_state=StockState.AVAILABLE,
        to_state=StockState.SCRAP,
        reason="Damaged beyond repair",
    )


def _dispose(warehouse, item, quantity: str, **header: object) -> ScrapDisposalCreate:
    return ScrapDisposalCreate.model_validate(
        {
            "warehouse_id": str(warehouse.id),
            "disposal_kind": "WRITE_OFF",
            "reason": "Authorised disposal of damaged metal",
            "lines": [{"item_id": str(item.id), "quantity": quantity}],
            **header,
        }
    )


class TestScrap:
    async def test_scrap_is_visible_then_disposed_at_what_it_cost(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        await _scrap(session, tenant, item, warehouse, "8")

        summary, _ = await InventoryQueryService(session, tenant).item_summary(item.id, PAGE)
        assert summary.total_scrap == Decimal("8")
        assert summary.warehouses[0].scrap_quantity == Decimal("8")
        # Owned stock nobody can use is reported beside the owned total, not in it.
        assert summary.total_owned_quantity == Decimal("12")

        # A later, dearer receipt must not re-price the scrap standing beside it.
        await _receive(session, tenant, item, warehouse, "5", "999")
        disposal = await ScrapDisposalService(session, tenant).create(
            _dispose(warehouse, item, "3")
        )

        assert disposal.status == "POSTED"
        assert disposal.total_value == Decimal("36.00")
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None
        assert balance.scrap_quantity == Decimal("5")
        assert balance.on_hand_quantity == Decimal("17")
        # Scrap carries no book value: disposing of it changes no stock value.
        assert balance.stock_value == Decimal("5139")

        [leg] = await _ledger(session, disposal.id)
        assert leg.transaction_type is TransactionType.SCRAP_DISPOSAL
        assert leg.quantity == Decimal("-3") and leg.total_cost == Decimal("36.00")
        assert leg.stock_state is StockState.SCRAP

        queue = await CostLayerRepository(session, tenant).open_layers_for_balance(
            balance.id, pool=CostLayerPool.SCRAP
        )
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in queue] == [
            (Decimal("5"), Decimal("12"))
        ]
        rows, _ = await ScrapDisposalService(session, tenant).list_disposals(
            ScrapDisposalFilters(), PAGE, UNSORTED
        )
        assert disposal.id in {row.id for row in rows}

    async def test_a_disposal_takes_the_oldest_scrap_first(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(session, company_id=tenant.company_id, code=unique_code("FO"))
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WF")
        )
        await _receive(session, tenant, item, warehouse, "10", "10")
        await _scrap(session, tenant, item, warehouse, "4")  # 4 @ 10
        await _receive(session, tenant, item, warehouse, "10", "20")
        await _scrap(session, tenant, item, warehouse, "8")  # 6 @ 10 + 2 @ 20

        disposal = await ScrapDisposalService(session, tenant).create(
            _dispose(warehouse, item, "11")
        )

        assert disposal.total_value == Decimal("120.00")  # 4@10 + 6@10 + 1@20
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None
        queue = await CostLayerRepository(session, tenant).open_layers_for_balance(
            balance.id, pool=CostLayerPool.SCRAP
        )
        assert [(layer.remaining_quantity, layer.unit_cost) for layer in queue] == [
            (Decimal("1"), Decimal("20"))
        ]

    async def test_scrap_without_a_cost_queue_is_reconciled_not_stranded(
        self, session, tenant, unique_code
    ) -> None:
        """Scrap that predates the queue - or that a missed movement left uncovered."""
        item, warehouse = await _stock(session, tenant, unique_code, cost="12")
        await _scrap(session, tenant, item, warehouse, "5")
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None
        balance_id = balance.id
        await session.execute(
            text("DELETE FROM inventory_cost_layers WHERE balance_id = :id AND pool = 'SCRAP'"),
            {"id": balance_id},
        )

        disposal = await ScrapDisposalService(session, tenant).create(
            _dispose(warehouse, item, "5")
        )

        # Priced at the position's recorded cost, and said so on the audit trail.
        assert disposal.total_value == Decimal("60.00")
        repairs = await session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == balance_id,
                AuditLog.entity_type == "inventory_balance",
            )
        )
        [repair] = repairs.scalars().all()
        assert repair.context is not None and repair.context["pool"] == "SCRAP"

    async def test_disposing_more_than_the_scrap_pool_holds_is_refused(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        await _scrap(session, tenant, item, warehouse, "2")
        item_id, warehouse_id = item.id, warehouse.id

        with pytest.raises(BusinessRuleError, match="in scrap"):
            await ScrapDisposalService(session, tenant).create(_dispose(warehouse, item, "3"))
        balance = await get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None and balance.scrap_quantity == Decimal("2")
        disposals = await session.execute(
            select(func.count())
            .select_from(ScrapDisposal)
            .where(ScrapDisposal.warehouse_id == warehouse_id)
        )
        assert disposals.scalar_one() == 0

    async def test_a_sale_records_its_buyer_and_what_it_realised(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        await _scrap(session, tenant, item, warehouse, "4")

        disposal = await ScrapDisposalService(session, tenant).create(
            _dispose(
                warehouse,
                item,
                "4",
                disposal_kind="SALE",
                buyer_reference="Shree Metals, slip 4471",
                realised_amount="150.00",
            )
        )

        assert disposal.disposal_kind == "SALE"
        assert disposal.buyer_reference == "Shree Metals, slip 4471"
        assert disposal.realised_amount == Decimal("150.00")
        assert disposal.total_value == Decimal("48.00")

    async def test_an_item_that_may_no_longer_be_scrapped_can_still_clear_its_scrap(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        await _scrap(session, tenant, item, warehouse, "2")
        item.can_scrap = False
        await session.flush()

        disposal = await ScrapDisposalService(session, tenant).create(
            _dispose(warehouse, item, "2")
        )
        assert disposal.status == "POSTED"

    async def test_an_item_that_may_not_be_scrapped_is_refused_and_nothing_moves(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, can_scrap=False)
        item_id, warehouse_id = item.id, warehouse.id

        with pytest.raises(BusinessRuleError, match="cannot be scrapped"):
            await _scrap(session, tenant, item, warehouse, "1")
        balance = await get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None
        assert (balance.on_hand_quantity, balance.scrap_quantity) == (20, 0)

    async def test_the_scrap_register_shows_both_directions_at_cost(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        await _scrap(session, tenant, item, warehouse, "8")
        await ScrapDisposalService(session, tenant).create(_dispose(warehouse, item, "3"))

        rows = (
            (
                await session.execute(
                    ReportQueryRepository(session, tenant).scrap_register(
                        _period(), ReportFilters(item_id=item.id)
                    )
                )
            )
            .mappings()
            .all()
        )
        assert sum(row["scrap_in_quantity"] for row in rows) == Decimal("8")
        assert sum(row["scrap_out_quantity"] for row in rows) == Decimal("3")
        [out] = [row for row in rows if row["scrap_out_quantity"]]
        assert out["disposal_kind"] == "WRITE_OFF" and out["total_cost"] == Decimal("36")
        [into] = [row for row in rows if row["scrap_in_quantity"]]
        assert into["total_cost"] == Decimal("96") and into["source_type"] == "stock_state_change"


# =============================================================================
# Internal issues
# =============================================================================


def _issue(warehouse, item, quantity: str, **header: object) -> InternalIssueCreate:
    return InternalIssueCreate.model_validate(
        {
            "warehouse_id": str(warehouse.id),
            "reason": "Issue materials to maintenance",
            "lines": [{"item_id": str(item.id), "quantity": quantity}],
            **header,
        }
    )


async def _second_person(session, tenant: TenantContext) -> TenantContext:
    approver = await seed_user(session, company_id=tenant.company_id)
    return replace(tenant, user_id=approver.id)


class TestInternalIssues:
    async def test_an_issue_under_the_approval_value_posts_and_is_listed(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="40")
        department = await _department(session, tenant, unique_code)
        service = InternalIssueService(session, tenant)

        draft = await service.create(_issue(warehouse, item, "5", department_id=str(department.id)))
        assert draft.status == "DRAFT" and not draft.requires_approval
        assert draft.total_value == Decimal("200.00")
        posted = await service.post(draft.id)

        assert posted.status == "POSTED" and posted.posted_at is not None
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None and balance.on_hand_quantity == Decimal("15")
        [leg] = await _ledger(session, draft.id)
        assert leg.transaction_type is TransactionType.MATERIAL_ISSUE
        assert leg.reference_type == "internal_issue" and leg.total_cost == Decimal("200.00")

        rows, _ = await service.list_issues(InternalIssueFilters(), PAGE, UNSORTED)
        assert draft.id in {row.id for row in rows}

    async def test_above_the_approval_value_it_takes_a_second_person(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="2000")
        department = await _department(session, tenant, unique_code)
        service = InternalIssueService(session, tenant)
        draft = await service.create(
            _issue(warehouse, item, "15", department_id=str(department.id))
        )
        issue_id, item_id, warehouse_id = draft.id, item.id, warehouse.id
        assert draft.requires_approval and draft.total_value == Decimal("30000.00")

        with pytest.raises(BusinessRuleError) as refused:
            await service.post(issue_id)
        assert refused.value.code is ErrorCode.APPROVAL_REQUIRED
        balance = await get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None and balance.on_hand_quantity == Decimal("20")

        await service.submit(issue_id)
        with pytest.raises(BusinessRuleError, match="someone other than"):
            await service.approve(issue_id)

        approver = await _second_person(session, tenant)
        approved = await InternalIssueService(session, approver).approve(issue_id)
        assert approved.status == "POSTED"
        assert approved.approved_by_id == approver.user_id
        assert approved.total_value == Decimal("30000.00")
        balance = await get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None and balance.on_hand_quantity == Decimal("5")

    async def test_a_draft_that_costs_more_when_it_posts_is_refused_and_nothing_moves(
        self, session, tenant, unique_code
    ) -> None:
        """The preview saw cheap stock; by posting time only dear stock is left."""
        item = await seed_item(session, company_id=tenant.company_id, code=unique_code("PV"))
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WP")
        )
        await _receive(session, tenant, item, warehouse, "10", "100")
        department = await _department(session, tenant, unique_code)
        service = InternalIssueService(session, tenant)
        draft = await service.create(
            _issue(warehouse, item, "10", department_id=str(department.id))
        )
        assert draft.total_value == Decimal("1000.00") and not draft.requires_approval
        issue_id, item_id, warehouse_id = draft.id, item.id, warehouse.id

        # Someone else issues the cheap stock, and dear stock arrives.
        await post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            quantity=Decimal("10"),
            transaction_type=TransactionType.MATERIAL_ISSUE,
            reference_type="test",
            reference_id=None,
            reference_code=None,
            reason="Issued elsewhere",
        )
        await _receive(session, tenant, item, warehouse, "10", "3000")

        with pytest.raises(BusinessRuleError) as refused:
            await service.post(issue_id)
        assert refused.value.code is ErrorCode.APPROVAL_REQUIRED
        balance = await get_balance(session, tenant, item_id, warehouse_id)
        assert balance is not None and balance.on_hand_quantity == Decimal("10")
        assert await _ledger(session, issue_id) == []

    async def test_a_cheap_draft_is_not_sent_for_approval(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        cost_centre = await _cost_centre(session, tenant.company_id, unique_code)
        service = InternalIssueService(session, tenant)
        draft = await service.create(
            _issue(warehouse, item, "1", cost_centre_id=str(cost_centre.id))
        )

        with pytest.raises(BusinessRuleError, match="posted directly"):
            await service.submit(draft.id)

    async def test_an_approver_rejects_a_pending_issue_with_its_reason(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="2000")
        department = await _department(session, tenant, unique_code)
        service = InternalIssueService(session, tenant)
        draft = await service.create(
            _issue(warehouse, item, "15", department_id=str(department.id))
        )
        issue_id = draft.id
        await service.submit(issue_id)
        with pytest.raises(BusinessRuleError, match="someone other than"):
            await service.reject(issue_id, "I changed my mind about this issue")

        approver = await _second_person(session, tenant)
        rejected = await InternalIssueService(session, approver).reject(
            issue_id, "Use the old stock in the maintenance cage first"
        )

        assert rejected.status == "CANCELLED"
        assert rejected.decision_note == "Use the old stock in the maintenance cage first"
        actions = await session.execute(
            select(AuditLog.action)
            .where(AuditLog.entity_id == issue_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        assert AuditAction.REJECT in actions.scalars().all()

    async def test_a_draft_is_cancelled_with_its_reason_and_a_posted_one_cannot_be(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        department = await _department(session, tenant, unique_code)
        service = InternalIssueService(session, tenant)
        first = await service.create(_issue(warehouse, item, "1", department_id=str(department.id)))
        second = await service.create(
            _issue(warehouse, item, "1", department_id=str(department.id))
        )
        posted_id = second.id

        cancelled = await service.cancel(first.id, "Raised twice by mistake")
        assert (
            cancelled.status == "CANCELLED" and cancelled.decision_note == "Raised twice by mistake"
        )

        await service.post(posted_id)
        with pytest.raises(InvalidStateTransitionError):
            await service.cancel(posted_id, "Too late")

    async def test_an_inactive_department_cannot_be_issued_to(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        department = await _department(session, tenant, unique_code, is_active=False)

        with pytest.raises(ValidationError, match="cannot be assigned"):
            await InternalIssueService(session, tenant).create(
                _issue(warehouse, item, "1", department_id=str(department.id))
            )

    async def test_another_tenants_cost_centre_is_not_found(
        self, session, tenant, unique_code, other_company
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code)
        foreign = await _cost_centre(session, other_company.id, unique_code)

        with pytest.raises(NotFoundError):
            await InternalIssueService(session, tenant).create(
                _issue(warehouse, item, "1", cost_centre_id=str(foreign.id))
            )

    async def test_consumption_by_department_adds_up_the_posted_issues_only(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="40")
        department = await _department(session, tenant, unique_code)
        cost_centre = await _cost_centre(session, tenant.company_id, unique_code)
        service = InternalIssueService(session, tenant)
        header = {"department_id": str(department.id), "cost_centre_id": str(cost_centre.id)}
        for quantity in ("2", "3"):
            draft = await service.create(_issue(warehouse, item, quantity, **header))
            await service.post(draft.id)
        await service.create(_issue(warehouse, item, "4", **header))  # a draft: not consumption

        [row] = (
            (
                await session.execute(
                    ReportQueryRepository(session, tenant).consumption_by_department(
                        _period(), ReportFilters(item_id=item.id)
                    )
                )
            )
            .mappings()
            .all()
        )
        assert row["department_code"] == department.code
        assert row["cost_centre_code"] == cost_centre.code
        assert (row["issue_count"], row["quantity"], row["total_cost"]) == (
            2,
            Decimal("5"),
            Decimal("200"),
        )


# =============================================================================
# Transfers and adjustments with a batch, a bin and a cost
# =============================================================================


class TestTransferAndAdjustmentLines:
    async def test_a_transfer_carries_its_batch_bins_and_cost(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session, company_id=tenant.company_id, code=unique_code("TB"), is_batch_tracked=True
        )
        source = await seed_warehouse(session, company_id=tenant.company_id, code=unique_code("TS"))
        destination = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("TD")
        )
        from_bin = await _bin(session, tenant.company_id, source, unique_code)
        to_bin = await _bin(session, tenant.company_id, destination, unique_code)
        batch = StockBatch(
            company_id=tenant.company_id,
            item_id=item.id,
            batch_number=unique_code("LOT"),
            received_on=today(),
        )
        session.add(batch)
        await session.flush()
        await _receive(
            session, tenant, item, source, "6", "17", location_id=from_bin.id, batch_id=batch.id
        )

        transfers = StockTransferService(session, tenant)
        transfer = await transfers.create(
            StockTransferCreate.model_validate(
                {
                    "from_warehouse_id": str(source.id),
                    "to_warehouse_id": str(destination.id),
                    "reason": "Move the lot to the dispatch bay",
                    "lines": [
                        {
                            "item_id": str(item.id),
                            "batch_id": str(batch.id),
                            "from_location_id": str(from_bin.id),
                            "to_location_id": str(to_bin.id),
                            "quantity": "4",
                        }
                    ],
                }
            )
        )
        await transfers.approve(transfer.id)
        await transfers.dispatch(transfer.id)
        received = await transfers.receive(transfer.id)

        assert received.lines[0].unit_cost == Decimal("17")
        position = await get_balance(
            session, tenant, item.id, destination.id, location_id=to_bin.id, batch_id=batch.id
        )
        assert position is not None
        assert (position.on_hand_quantity, position.stock_value) == (4, Decimal("68"))

    async def test_an_adjustment_carries_batch_bin_and_cost_and_its_draft_can_be_edited(
        self, session, tenant, unique_code
    ) -> None:
        item = await seed_item(
            session, company_id=tenant.company_id, code=unique_code("AB"), is_batch_tracked=True
        )
        warehouse = await seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("AW")
        )
        rack = await _bin(session, tenant.company_id, warehouse, unique_code)
        batch = StockBatch(
            company_id=tenant.company_id,
            item_id=item.id,
            batch_number=unique_code("LOT"),
            received_on=today(),
        )
        session.add(batch)
        await session.flush()
        payload = {
            "warehouse_id": str(warehouse.id),
            "reason_code": "CORRECTION",
            "reason": "Counted correction",
            "lines": [
                {
                    "item_id": str(item.id),
                    "batch_id": str(batch.id),
                    "location_id": str(rack.id),
                    "quantity": "2",
                    "unit_cost": "19",
                }
            ],
        }
        adjustments = StockAdjustmentService(session, tenant)
        draft = await adjustments.create(StockAdjustmentCreate.model_validate(payload))
        assert draft.total_value == Decimal("38.00")

        payload["lines"][0]["quantity"] = "3"  # type: ignore[index]
        edited = await adjustments.update(draft.id, StockAdjustmentUpdate.model_validate(payload))
        assert edited.total_value == Decimal("57.00")
        assert [(line.quantity, line.batch_id, line.location_id) for line in edited.lines] == [
            (Decimal("3"), batch.id, rack.id)
        ]
        await adjustments.post(draft.id)
        item_id, warehouse_id, rack_id, batch_id, draft_id = (
            item.id,
            warehouse.id,
            rack.id,
            batch.id,
            draft.id,
        )
        position = await get_balance(
            session, tenant, item_id, warehouse_id, location_id=rack_id, batch_id=batch_id
        )
        assert position is not None
        assert (position.on_hand_quantity, position.stock_value) == (3, Decimal("57"))

        with pytest.raises(BusinessRuleError, match="Only a draft"):
            await adjustments.update(draft_id, StockAdjustmentUpdate.model_validate(payload))

    async def test_an_approver_rejects_and_the_raiser_cancels(
        self, session, tenant, unique_code
    ) -> None:
        item, warehouse = await _stock(session, tenant, unique_code, cost="5000")
        payload = {
            "warehouse_id": str(warehouse.id),
            "reason_code": "DAMAGE",
            "reason": "Crushed in the racking",
            "lines": [{"item_id": str(item.id), "quantity": "-6"}],
        }
        adjustments = StockAdjustmentService(session, tenant)
        pending = await adjustments.create(StockAdjustmentCreate.model_validate(payload))
        assert pending.requires_approval
        await adjustments.submit(pending.id)
        approver = await _second_person(session, tenant)

        rejected = await StockAdjustmentService(session, approver).reject(
            pending.id, "Photograph the damage first"
        )
        assert rejected.status == "REJECTED"
        assert rejected.decision_note == "Photograph the damage first"

        another = await adjustments.create(StockAdjustmentCreate.model_validate(payload))
        cancelled = await adjustments.cancel(another.id, "Duplicate count entry")
        assert cancelled.status == "CANCELLED"
        balance = await get_balance(session, tenant, item.id, warehouse.id)
        assert balance is not None and balance.on_hand_quantity == 20

    async def test_an_internal_issue_is_not_an_adjustment_status(self) -> None:
        """Pinned so the two vocabularies cannot be merged by accident."""
        assert "REJECTED" not in {status.value for status in InternalIssueStatus}
