I have everything needed. Here is the report.

# Customer returns (gap 4): inventory, dispatch and quality seams

> **Post-E8b note - 22 October 2026.** This report is the pre-E8b audit behind
> A3 and its recommendations are left as written. E8b later added serial
> capture to goods receipts, production and assembly output, transfers and
> adjustments, plus the `/inventory/tracking` register. Statements below that
> those paths refuse serial stock are therefore historical. The return-specific
> gap this report identifies - per-serial QC disposition - remains; the
> credit-note link has since been implemented.

**Verdict on the audit claim.** The audit is right that there is no customer-return **document**. Nothing creates, prices, posts or lists a sales return, and the frontend has no screen for one.

It would be wrong to read that as "nothing exists", though. Most of the mechanics underneath are already built:
- The inventory engine fully implements the `CUSTOMER_RETURN` movement, the `RETURN` stock state and the `RETURNED` serial status. All three are covered by integration tests.
- Quality has a `RETURN` stage and already maps `"sales_return"` and `"customer_return"` to it.
- Warehouses have a `RETURN` type, and the seed creates `WH-RET "Sales Return Store"`.
- Items carry a `can_return` flag, but nothing ever checks it.
- Finance has a credit-note API with no screen.

What is missing is the document that drives these pieces. Today no HTTP path can bring dispatched goods back as a return. For serial-tracked goods there is no HTTP path back into stock at all.

---

## Exists today

### 1. Inventory engine: `CUSTOMER_RETURN` works at engine level

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\inventory\models.py`**
- L103-133 `class TransactionType(StrEnum)`. L118 `CUSTOMER_RETURN = "CUSTOMER_RETURN"`. Siblings: L116 `MATERIAL_RETURN`, L128 `PURCHASE_RETURN`.
- L142-162 `TRANSACTION_SIGNS`. L148 `CUSTOMER_RETURN: 1`. `INCREASE_TYPES` and `DECREASE_TYPES` (L164-169) are derived from it.
- L203-219 `class StockState`: `AVAILABLE, QC_HOLD, DAMAGED, IN_TRANSIT, JOB_WORK, SCRAP, RETURN`.
  - Docstring L206-207: *"`AVAILABLE` and `RETURN` are the free pool: physically here and usable (a customer return is ordinary stock until somebody puts it on hold)."*
- L224-232 `STATE_BUCKETS`. L226 `StockState.RETURN: None`, so RETURN has **no balance column**.
- L243-245 `ON_HAND_STATES = {AVAILABLE, RETURN, QC_HOLD, DAMAGED}`.
- L538-545 `available_quantity` is a generated column: `on_hand - reserved - qc_hold - damaged`.
- **What this means:** stock landed in `RETURN` counts as available, can be reserved, and the balance row cannot tell it apart from AVAILABLE. `RETURN` is only a ledger label. To keep unchecked returns out of availability, use `QC_HOLD` or `DAMAGED`, or a separate warehouse.
- L255-260 `SerialStatus`: `IN_STOCK, ISSUED, DISPATCHED, RETURNED, SCRAPPED`.
- L270-272 `SERIAL_IN_WAREHOUSE_STATUSES = {IN_STOCK, RETURNED}`.
- L415-423 `InventoryTransaction.source_state`, plus `reference_type` (String 48), `reference_id` (bare UUID) and `reference_code`.

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\inventory\service.py`**

`MovementRequest` (L255-330, frozen). Exact fields:
- Required: `item_id`, `warehouse_id`, `quantity` (a magnitude), `transaction_type`, `reference_type`, `reason`.
- Optional, with defaults: `reference_id=None`, `reference_code=None`, `location_id=None`, `batch_id=None`, `serial_id=None`, `unit_cost=None`, `stock_state=AVAILABLE`, `from_state=None`.
- `completes_in_transit=False`. `__post_init__` (L310-321) refuses it on anything but `TRANSFER_IN`, so returns get no exemption.
- `cost_layers: tuple[LayerCost, ...]=()` is an exact FIFO breakdown for an arrival.
- `carries_cost_of: int | None` and `occurred_at=None`.

Movement rules:
- L373 `CUSTOMER_RETURN: _Transition(None, StockState.AVAILABLE, "target")`. The caller's `stock_state` overrides the landing pool:
  - `QC_HOLD` raises on_hand and qc_hold.
  - `DAMAGED` raises on_hand and damaged.
  - `RETURN` raises on_hand only.
- `signed_quantity` (L403-429) refuses a negative receipt.
- `resolve_pools` (L432-478) and `balance_effect` (L481-493) turn type plus pools into column changes.
- `state_change_type` (L509-545):
  - RETURN→AVAILABLE, QC_HOLD→AVAILABLE and QC_HOLD→RETURN all post `QC_RELEASE`.
  - Moves into QC_HOLD or DAMAGED post `QC_HOLD`.
  - Moves into SCRAP post `SCRAP`.
  - IN_TRANSIT and "from SCRAP" are refused.

Valuation:
- `inbound_unit_cost` (L714-742) picks a price in this order: the caller's `unit_cost` if > 0, then `item.standard_cost`, then the position's cost basis, then the last average.
  - Its docstring says a return that names no cost is re-booked at standard cost: *"Callers that know the number should pass it."*
- `value_movement` (L814-911) and `_layers_to_open` (L914-942). A carried breakdown that falls short is topped up; one that is larger than the quantity raises `ValidationError`.
- `_write_layers` ASSUMPTION (L2899-2909): *"every arrival opens a **new** layer ... A material return, a customer return and a reversal are all arrivals ... a document that knows what it issued passes that cost back."*
- `_apply` (L2728-2878) uses `fallback_cost=item.standard_cost` (L2804) and stores `source_state` (L2839).

`validate_movement` (L1242-1430). A `CUSTOMER_RETURN` has no source pool, so `gates_policy` is True (L1289). It is refused when:
- the item is inactive (L1294-1298), the warehouse is inactive (L1299-1303) or the bin is retired (L1324-1332);
- the bin is in another warehouse (L1310-1319);
- a batch-tracked item has no batch (L1366-1371, `BATCH_REQUIRED`);
- a serial-tracked item has no serial (L1401-1409, `SERIAL_REQUIRED`);
- a serial movement's quantity is not 1 (L1425-1430).

Serial lifecycle:
- `SERIAL_FLOW` (L1618-1680):
  - `DISPATCHED→RETURNED` (L1650-1654) is the only way back from a customer.
  - From RETURNED a unit can go to `IN_STOCK`, `ISSUED`, `DISPATCHED` or `SCRAPPED` (L1657-1677).
- `SERIAL_MOVEMENT_TARGETS` (L1701-1724). L1710 `CUSTOMER_RETURN: RETURNED`; `QC_HOLD` and `QC_RELEASE` map to `None`.
- `serial_transition` (L1842-1931). L1900-1906: a `QC_RELEASE` landing in `AVAILABLE` turns a `RETURNED` unit into `IN_STOCK`. Landing in RETURN or DAMAGED leaves it `RETURNED`.
- `movement_relocates_unit` (L1727-1750) and `_advance_serial` (L3065-3067). A `CUSTOMER_RETURN` rewrites the unit's recorded warehouse and bin to the return position.
- `assert_unit_is_where_the_movement_is` (L1767-1839). A movement that does not relocate the unit (a release, a hold) must be raised at the unit's recorded warehouse.

Public API signatures (verbatim):
- `post_movement(session, tenant, *, item_id, warehouse_id, quantity, transaction_type, reference_type, reference_id, reference_code, reason, location_id=None, batch_id=None, serial_id=None, unit_cost=None, stock_state=StockState.AVAILABLE, occurred_at=None) -> InventoryTransaction` (L3301-3342).
- `post_movements(session, tenant, movements: list[MovementRequest]) -> list[InventoryTransaction]` (L3345-3355).
- `change_stock_state(session, tenant, *, item_id, warehouse_id, quantity, from_state, to_state, reason, location_id=None, batch_id=None, serial_id=None, reference_type="stock_state_change", reference_id=None, reference_code=None, occurred_at=None) -> InventoryTransaction` (L3478-3520). It prefixes the reason with `"FROM -> TO: "`.

`post_with_balances` (L2146-2256) does, in order:
1. Validate master data.
2. Lock balances in primary-key order.
3. Lock the open cost layers.
4. Reconcile layers.
5. Lock serials.
6. Apply the movements.
7. Flush once.
8. Write one `INVENTORY_MOVEMENT` audit row per movement, plus serial audit rows.

Other pieces a return will use:
- Exact cost replay precedent: `_carried_layers` (L3845-3862) and `StockTransferService.receive` (L4122-4204), using `by_transaction` (L4151-4157) and `cost_layers=` (L4172).
- `assert_warehouse_scope(tenant, *warehouse_ids)` (L1180-1199).

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\inventory\repository.py`**
- `BalanceGrain(item_id, warehouse_id, location_id=None, batch_id=None)` (L70-98). `batch_id` is part of the grain; `serial_id` is not.
- `ItemFacts` (L119-135) has no `can_return`.
- `CostConsumptionRepository.by_transaction(ids)` (L906-938) orders by `(created_at, id)`.
- `LedgerRepository.posted_quantity` (L715-776) is the reconciliation identity.

**Tests that already pin this engine behaviour**
- `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_inventory_serials.py`:
  - L381-417 `test_the_whole_journey_of_one_unit` (… DISPATCH → CUSTOMER_RETURN → SCRAP).
  - L1301-1389 `TestACustomerReturnCanBeSoldAgain`.
  - L1392-1514 `TestTheStatusFollowsThePoolNotTheDocumentType`.
- `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_inventory_serial_lifecycle.py`: L129-135, L294-301, L388-391 (a customer return of an `ISSUED` unit is refused), L446-486.
- `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_inventory_movement_rules.py`: signs are parametrised over all types; L436 pins the batch-versus-serial direction rule.

**Who calls it: nobody.** `CUSTOMER_RETURN` appears in no module except inventory.
- The inventory router has no generic "post a movement" endpoint.
- The only HTTP way to put goods back is a stock adjustment (`ADJUSTMENT_INCREASE`).
  - `AdjustmentReason` (models.py L294-307) has no return reason.
  - `StockAdjustmentLine` (L1280-1330) has no `serial_id`.

### 2. Warehouses
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\warehouses\models.py` L47-66 `WarehouseType` includes `RETURN` (L63).
  - Docstring: it is a classification. *"no rule reads it to decide whether a movement is legal... It drives defaults (where a GRN proposes to receive), reporting groups, and the shop-floor pickers."*
  - No module outside warehouses reads `warehouse_type`.
- `C:\Project\Other\ATCRAFT\apps\api\src\core\seed.py` L147 seeds `{"code": "WH-RET", "name": "Sales Return Store", "warehouse_type": "RETURN"}`.

### 3. Quality

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\quality\models.py`**
- `QcStage` (L104-118) includes `RETURN` (L118); the database CHECK allows it.
- `InspectionResult` (L154-174), `InspectionStatus` (L177-196), and the DECIDED / OPEN / REOPENABLE groups (L202-223).
- `QcTemplate.qc_stage` (L252). A template can target stage RETURN. One active template per (item, stage), (category, stage) or stage default (L285-311).
- `QcInspection` (L426-637):
  - Has `warehouse_id` (L446-448), `batch_id` (L452-454), `vendor_id` (L475-477, read by the vendor rating), `reference_type/id/code` (L483-485), the disposition quantities, and `return_required` (L544-546, set only when `vendor_id` is present).
  - Has **no `location_id`, no `customer_id` and no serial list.**

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\quality\service.py`**
- `STAGE_BY_REFERENCE_TYPE` (L183-195) already has `"sales_return": QcStage.RETURN` and `"customer_return": QcStage.RETURN`. Nothing passes either reference type today.
- Every creation path goes through `_raise_inspection` (L1341-1463). It numbers with `DocTypes.QC_INSPECTION`, audits `CREATE`, and holds stock only when `hold_stock=True` (L1406-1424).
- Entry points:
  - `create_incoming_inspection(session, tenant, *, item_id, quantity, warehouse_id, reference_type, reference_id, reference_code, vendor_id=None, batch_id=None)` (L1466-1502), stage INCOMING.
  - **`create_in_process_inspection(...)`, same signature** (L1505-1543). The stage is `STAGE_BY_REFERENCE_TYPE.get(reference_type, IN_PROCESS)`. This is the seam a sales return should call.
  - `create_inspection(session, tenant, payload: InspectionCreate)` (L1546-1566) is the manual path; `hold_stock` moves AVAILABLE→QC_HOLD.
- `_prepare` (L1269-1338) refuses an inactive item (L1293-1297), an inactive warehouse (L1303-1306) and a batch-tracked item with no batch (L1308-1314). With no template it inspects 100%.
- `decide_inspection(session, tenant, inspection_id, payload: DecideRequest) -> DecisionResponse` (L1735-1881).
  - Its stock effect is `_post_decision_movements` (L1884-1930): `change_stock_state(QC_HOLD → AVAILABLE | DAMAGED | SCRAP, batch_id=inspection.batch_id, reference_type="qc_inspection")`. It passes **no `location_id` and no `serial_id`**.
  - REWORK and HOLD move nothing (`STATE_FOR_DISPOSITION`, L1704-1713).
  - L1825 sets `return_required = rejected > 0 and vendor_id is not None`.
  - `_announce_failure` (L1933-1988) notifies the QC roles, and the purchase roles only when a vendor is named.
- **The owning document is never told the verdict.** There is no callback. Procurement reads it back with `inspections_for_reference(session, tenant, reference_type, reference_id) -> list[QcInspection]` (L2072-2082). The repository's `list_for_reference` (quality\repository.py L413-432) orders by `(created_at, id)`.
  - Nothing ever sets `ReceiptLineQcStatus.PASSED` or `FAILED` on a GRN line.

How other modules call quality (a "port" that tolerates the module being absent but never swallows a failure inside it):
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\service.py`:
  - L300-321 `QualityPort` (Protocol including `vendor_id` and `batch_id`); L324-338 `resolve_quality_port()`.
  - L341-366 `InspectionReaderPort` and `resolve_inspection_reader_port()`.
  - L369-386 `_inspection_outcome` reads the quantities with `getattr`.
  - L3219-3249 `_raise_inspections` stores `line.qc_inspection_id` and sets `qc_status=PENDING`.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\job_work\service.py`:
  - L700-736 port to `create_in_process_inspection`; L2153-2177 raises the inspection.
  - It **also** has `record_qc_result` (L2316-2450), a second, owner-side release path that posts `QC_HOLD→AVAILABLE/DAMAGED` itself.
- Assembly uses the same port (`assembly\service.py` L592-606).

### 4. Dispatch

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\dispatch\models.py`**
- `DispatchType` (L88-102): SALES, PURCHASE_RETURN, JOB_WORK, TRANSFER. All outbound.
- `DispatchStatus` (L105-129): NOT_DISPATCHED → READY_FOR_DISPATCH → DISPATCHED → IN_TRANSIT → DELIVERED → QC_PENDING → RECEIVED → QC_COMPLETED → GRN_COMPLETED, plus CANCELLED.
  - Groups: `IN_FLIGHT_STATUSES` (L162-165), `DELIVERED_STATUSES` (L169-175), `EDITABLE_STATUSES` (L180-183).
- `Dispatch(TenantEntity, OptimisticLockMixin)` (L186-399):
  - `customer_id` (L216-218).
  - `warehouse_id` NOT NULL (L229-231). It is where the stock left from.
  - CHECK that a SALES dispatch names a customer (L384-387), and that a sales order sits only on a SALES dispatch (L395-398).
- `DispatchLine` (L402-481):
  - `sales_order_line_id` (nullable, RESTRICT, L419-421), `item_id`, `quantity`, `batch_id` (FK, L428-430).
  - `serial_ids uuid[]` (L433-435), with a GIN index (L465) and CHECK `cardinality(serial_ids) = 0 OR = quantity` (L477-480).
  - `carton_ids`, `reservation_id` (bare, L448) and `inventory_transaction_id` (bare, L452).
  - **No `location_id`, no cost or price, no `returned_quantity`.**

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\dispatch\service.py`**
- `DISPATCH_LIFECYCLE` (L227-320). Ready→Dispatched needs `dispatch.create`; →Delivered needs `dispatch.confirm_delivery`. There is no return edge.
- `build_line_movements` (L547-582):
  - one DISPATCH movement per line, or one per serial (quantity 1);
  - `stock_state=AVAILABLE`, `batch_id=line.batch_id`, **no location**;
  - `reference_type="dispatch"` (L149).
- `dispatch_consignment` (L1390-1631):
  - Lock order: dispatch header, then sales-order lines by primary key.
  - Reserved lines go through `allocation.consume(..., transaction_type=DISPATCH)` (L1514-1524). **`line.inventory_transaction_id` is never set for these** (L1525-1527).
  - Direct lines get the first posted ledger row as their anchor (L1540-1547). A serial line posts several rows.
  - Then sales-order lines and status are advanced (L1550-1576), the status changes, and an `INVENTORY_MOVEMENT` audit row is written with `LedgerLookupRepository.posted_for` (L1597-1622).
- `confirm_delivery` (L1696-1777).
- `_validate_lines` (L696-848) requires a serial to be **exactly `IN_STOCK`** (L803-808) and at the dispatch warehouse (L809-813). `packing\service.py` L827-831 has the same `IN_STOCK` check.

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\dispatch\repository.py`**
- `LedgerLookupRepository.posted_for(reference_type, reference_id)` (L390-413), using the ledger's reference index.
- `DispatchLineRepository` (L484-526) and `SerialLookupRepository` (L346-371).
- `DispatchFilters` (schemas.py L454-473) has no "has left the building" filter. `in_flight_only` excludes delivered consignments.

**Sales order line**
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\sales\models.py`, `SalesOrderLine` (L290-431):
  - `dispatched_quantity` (L362-364) and `invoiced_quantity`;
  - a frozen CGST/SGST/IGST split;
  - CHECK `dispatched_within_ordered` (L418);
  - an open-demand partial index (L390-395);
  - **no `returned_quantity`**.
- Sales reservations are warehouse-level: `allocation.reserve` (L933-1026) receives no batch or location from sales.

### 5. Purchase return: the structural precedent

**Models**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\models.py`
- `PurchaseReturnStatus` (L232-235): DRAFT, POSTED, CANCELLED. `RETURNABLE_STOCK_STATES = ("AVAILABLE", "DAMAGED")` (L242).
- `GoodsReceiptLine.returned_quantity` (L1235-1237), with CHECK `returned_within_accepted` (L1313).
- `PurchaseReturn(TenantEntity)` (L1336-1415):
  - `code`, `vendor_id`, **`goods_receipt_id` NOT NULL**, `purchase_order_id`, `warehouse_id`, `return_date`, `status` String(24);
  - `reason` NOT NULL, `debit_note_number`, `currency`, `subtotal` / `tax_amount` / `total_amount`;
  - `posted_at`, `posted_by_id`, `cancellation_reason`, `notes`;
  - CHECKs: status known, posted rows carry evidence, cancelled rows carry a reason.
- `PurchaseReturnLine` (L1418-1493):
  - **`goods_receipt_line_id` NOT NULL**, `item_id`, `quantity`, `uom_id`, `unit_price`, `tax_rate`, `tax_amount`, `line_total`;
  - `batch_id` (bare), `location_id`, `stock_state` with CHECK `IN ('AVAILABLE','DAMAGED')`, `reason`, `notes`.
- Migration `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\20260905_0000_purchase_return_line_stock_state.py` (revision `3c8f2a7d5b91`) is the review template: locking impact, data migration, rollback.

**State machine**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\service_rules.py`
- `PURCHASE_RETURN_LIFECYCLE` (L408-427): DRAFT→POSTED (`purchase_return.create`, irreversible); DRAFT→CANCELLED; both terminal.
- Pool rules: `InspectionOutcome` (L1258), `ReturnPoolFacts` (L1278), `returnable_by_pool` (L1296-1317), `resolve_return_pool` (L1320-1387).

**Service**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\service.py`
- Constants: `AUDIT_PURCHASE_RETURN = "purchase_return"` (L211), `MOVEMENT_REFERENCE_RETURN = "purchase_return"` (L216).
- `_transition` (L432-469) is the single status writer: `assert_transition`, assign, flush, then `record_status_change`.
- `create_purchase_return(session, tenant, payload) -> tuple[PurchaseReturn, list[PurchaseReturnLine]]` (L3383-3530):
  - the receipt must be POSTED;
  - `lock_many` the receipt lines;
  - each line must be on that receipt;
  - returnable = `accepted_quantity - returned_quantity`;
  - resolve the pool;
  - price at the receipt's `unit_cost`, tax at the item's GST rate, sum the totals;
  - audit `CREATE`.
- `post_purchase_return(session, tenant, principal, return_id) -> PurchaseReturnPostResult` (L3533-3703):
  1. Lock the header and assert DRAFT→POSTED.
  2. Lock the receipt lines and re-derive the pool facts.
  3. Lock the purchase-order lines **before** `post_movements` (deadlock order, L3581-3594).
  4. Re-check returnable quantity under the lock.
  5. Build one `MovementRequest(PURCHASE_RETURN, reference_type="purchase_return", batch_id, location_id, stock_state=AVAILABLE|DAMAGED)` per line and call `post_movements`.
  6. Add to `returned_quantity` on the receipt line and the order line.
  7. Write posting evidence, call `_transition`, and audit `INVENTORY_MOVEMENT`.
- `cancel_purchase_return` (L3706-3729), list (L3732-3742), detail (L3745-3752).
- `return_detail` (L3375-3380) validates the header, **then** assigns the lines. `PurchaseReturn` has no `lines` relationship.

**Repository**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\repository.py`
- `GoodsReceiptLineRepository.lock_many` (L1124-1134): FOR UPDATE, ordered by id.
- `PurchaseReturnLineRepository` (L1232-1288). `posted_quantities_by_pool` returns `{}` for an empty id list.
- `PurchaseReturnRepository` (L1291-1328): `lock_for_update`, a `sortable_fields` whitelist, and `list_page_filtered` using `sort.resolve(...)`.

**Router**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\router.py` (L1155-1237)
- `GET` and `POST /procurement/purchase-returns`, `GET /{id}`, `POST /{id}/post`, `POST /{id}/cancel`.
- View needs `purchase_return.view`; create, post and cancel all need `purchase_return.create`.

**Schemas**, `C:\Project\Other\ATCRAFT\apps\api\src\modules\procurement\schemas.py`
- Shared types: `PositiveQuantity` (L54), `Reason` (L62), `ReturnStockState` Literal (L67), `ReasonRequest` (L761-766), `PostedMovement` (L1051-1059).
- `PurchaseReturnLineIn` (extra=forbid, L1080-1094), `PurchaseReturnLineRead` (L1097-1116).
- `PurchaseReturnCreate` (L1119-1138) has a validator: unique line numbers, and each receipt line at most once.
- `PurchaseReturnSummary`, `PurchaseReturnDetail`, `PurchaseReturnFilters` and `PurchaseReturnPostResult` (L1141-1181).

**Tests**
- `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_procurement_service.py`:
  - L816-856 posts a negative movement against the receipt line;
  - L858-880 cannot exceed the accepted quantity;
  - L883-921 two returns cannot exceed it between them;
  - L924-940 an unposted receipt is refused;
  - L1527-1582 a rejected lot is returned from damaged stock;
  - L1585-1611 a line still on QC hold is refused.
- `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_procurement_return_pools.py`: pure pool rules.
- `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_procurement_lifecycle.py` L267-272: a posted return is terminal.

### 6. Allocation
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\allocation\service.py`:
  - `CONSUMPTION_MOVEMENT` (L717-726): `SALES_ORDER → DISPATCH`.
  - `ReservationEngine.promisable` (L811-828) is `on_hand - reserved - qc_hold - damaged`. RETURN-state stock is promisable; QC_HOLD stock is not.
  - `consume` (L1121-1232) posts the DISPATCH but returns the reservation, not the ledger row.
- There is no return logic. A consumed reservation stays consumed, and a return must not reopen it.

### 7. `can_return`
- Column: `C:\Project\Other\ATCRAFT\apps\api\src\modules\products\models.py` L417-419, default `True`.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\products\service.py`: listed in `CAPABILITY_FIELDS` (L163-173); `default_capabilities` (L251-289) sets it True, except SERVICE items (False, L287).
- **It is enforced nowhere.** Purchase returns don't check it, and neither the procurement `PurchasableItem` nor the sales `SellableItem` projection reads it. The precedent for enforcing a capability flag is the `can_sell` check in `sales\service.py` L728-740.

### 8. Finance seams
- Credit note API:
  - `C:\Project\Other\ATCRAFT\apps\api\src\modules\finance\service.py` `post_customer_adjustment(session, tenant, principal, customer_id, payload: CustomerAdjustmentCreate) -> CustomerAdjustmentRead` (L3189-3315). Its entries come from `customer_adjustment_lines` (L3150-3186).
  - `CustomerAdjustmentType.CREDIT` (finance\models.py L150-161).
  - The route `POST /finance/customer-adjustments` is gated on `payment.approve` (assumptions §32).
  - The handbook confirms: *"a debit-note and credit-note facility ... but it has no screen"*.
- There is no sales-revenue or sales-returns account. `SystemAccount` (models.py L164-180) and `DEFAULT_CHART` (service.py L477-492) have none.
- Output GST is computed from dispatch lines: `C:\Project\Other\ATCRAFT\apps\api\src\modules\finance\repository.py` `TAXABLE_DISPATCH_STATUSES` (L145) and `GstRepository.output_rows_stmt` (L1719-1764). There is no reversal for returns.

### 9. Frontend
- Purchase-return screens, the UI precedent (`C:\Project\Other\ATCRAFT\apps\web\src\features\procurement\`):
  - `purchase-return-model.ts`: keys under the procurement prefix (L55-60); statuses with `satisfies` (L85-89); `isReturnPostable` / `isReturnCancellable` mirroring the machine (L104-106); `returnableQuantity` (L212-215).
  - `purchase-returns-page.tsx`: `FILTER_KEYS` (L77-84); `canManage = can(P.PURCHASE_RETURN_CREATE)` (L104); `useListQuery` (L117-134); columns with sorting disabled where the API won't sort; the "Stock" column keeps POSTED and CANCELLED apart.
  - `purchase-return-dialogs.tsx`:
    - `PostedReceiptPicker` (L121) searches the server with `status: 'POSTED'`.
    - The zod `lineSchema` and `superRefine` (L259-316) check returnable quantity.
    - `isSentLine` and `remapArrayIndices` route server errors back to the right line.
    - The form mutation (L428-474) invalidates only the return keys.
    - The post dialog (L1035-1204) invalidates `queryKeys.procurement.all` and `queryKeys.inventory.all`.
    - The cancel dialog (L1215-1287) collects a reason.
  - `purchase-returns-page.test.tsx` uses `renderScreen` and `RouteTable` from `@/test/harness`.
- Wiring:
  - `app/router.tsx` L339-342; `app/route-loaders.ts` L51; `app/navigation.ts` ~L179-184; `lib/permissions.ts` L196-197; `features/audit/audit-types.ts` L80.
- Vocabulary already present: `features/inventory/types.ts` has `StockState` including RETURN (L104-106) and `INFLOW_TRANSACTION_TYPES` including CUSTOMER_RETURN (L141-152). `components/shared.tsx` `STATUS_VARIANTS` already has DRAFT, RECEIVED, QC_PENDING, CLOSED, POSTED, CANCELLED, RETURN and RETURNED.
- Dispatch screens: `features\dispatch\dispatch-page.tsx` (gates at L300-305), `dispatch-dialogs.tsx`, `dispatch-types.ts`. None has a return action.

---

## Missing vs proposal (gap 4)

1. **No return document.** No tables, model, state machine, number series, permissions, routes, service or screen.
2. **No link from a return to a dispatch.**
   - `dispatch_lines` has no `returned_quantity`.
   - Nothing computes returnable quantity or returnable serials.
   - No dispatch-list filter for "goods have left" (in flight plus delivered).
3. **No caller of the engine path.** Nothing posts `CUSTOMER_RETURN` or passes reference type `"sales_return"`.
   - Serial-tracked goods cannot come back at all: adjustments carry no serial, and there is no other movement endpoint.
   - Batch-tracked goods can only come back by adjustment, which loses traceability.
4. **No return costing.** Nothing reads the original DISPATCH cost. A return posted without a cost falls back to standard cost (`inbound_unit_cost`).
5. **QC: the stage and template plumbing exist, but there is no hookup and no serial support.**
   - `QcInspection` has no serial list.
   - The decision posts no `serial_id`.
   - A SCRAP decision on held serial-tracked stock would be refused: `SCRAP` is serial-gated (`validate_movement` L1401-1409).
   - An accepted unit stays `RETURNED`, and dispatch and packing refuse it.
6. **No credit-note link.** No credit value is computed from the sales-order line, no reversal of output GST, no contra account, and no screen for `POST /finance/customer-adjustments`.
7. **`can_return` is not enforced.**
8. **Nothing records why a return happened.** No return reason codes and no per-customer return or quality figures (`qc_inspections` has no `customer_id`), so no dealer-performance input from returns.

---

## Patterns to copy

| Piece | Precedent (absolute path) | Conventions to follow |
|---|---|---|
| Header and line tables | `...\procurement\models.py` L1336-1493 (`PurchaseReturn`, `PurchaseReturnLine`) | `TenantEntity`; `code String(64)` with `UniqueConstraint("company_id","code")`; status `String(24)` plus CHECK `status IN (sql_in_list(Enum))`; CHECKs for posting evidence and cancel reason; parent FK on lines NOT NULL RESTRICT; `UniqueConstraint(parent_id, line_number)`; `Quantity`, `UnitPrice`, `Money`, `Percentage` types; lines loaded by repository, **no `lines` relationship** |
| Serial set on a line | `...\dispatch\models.py` L433-435, L465, L477-480 | `ARRAY(GUID)` with `server_default '{}'::uuid[]`, GIN index, CHECK cardinality equals quantity |
| Running "already returned" total | `GoodsReceiptLine.returned_quantity` L1235-1237 plus CHECK L1313 | Written only by posting, under `lock_many` (FOR UPDATE ordered by id) |
| State machine | `...\procurement\service_rules.py` L408-427 | `StateMachine(entity="Sales return", ...)` (must pass `test_state_machine_entity_names` regex); `Transition(src, dst, permission=..., label=..., is_irreversible=True)` on the stock edge; single writer `_transition` (procurement\service.py L432-469) |
| Numbering | `C:\Project\Other\ATCRAFT\apps\api\src\common\codes.py` L65-91 | Add `SALES_RETURN = DocType("sales_return", "SRTN")` (yearly; statutory); call `next_document_number(session, company_id, DocTypes.SALES_RETURN, on=return_date)` inside the create transaction; no migration needed |
| Create, post, cancel services | procurement\service.py L3383-3729 | `async with transaction(session)`; lock header, then dependent lines, then `post_movements`; re-check quantities under lock; `record_audit(AuditAction.CREATE / INVENTORY_MOVEMENT)`; `record_status_change` via `_transition`; cancel uses `AuditAction.CANCEL` |
| Return priced at issue cost | `C:\Project\Other\ATCRAFT\apps\api\src\modules\production\service.py` `_post_return_lines` L2627-2698 (`unit_cost = net_cost / net_quantity` L2660-2665) | Pass `unit_cost` explicitly; exact-layer alternative: `_carried_layers` and `cost_layers=` (inventory\service.py L3845-3862, L4151-4172) |
| Movement per serial | `build_line_movements` (dispatch\service.py L547-582) | One `MovementRequest(quantity=1, serial_id=s)` per unit |
| Receive into QC hold, then raise inspection | `post_goods_receipt`, `_build_receipt_movements` and `_raise_inspections` (procurement\service.py L2960-3249) | `stock_state=QC_HOLD`; one inspection per held line through a `resolve_quality_port()` Protocol; store `qc_inspection_id` (bare UUID) on the line |
| Read the verdict back | `_return_pool_facts` and `_inspection_outcome` (procurement\service.py L369-424) plus `resolve_inspection_reader_port` (L353-366) | Pull model via `inspections_for_reference`; never import quality's types |
| Owner-side QC verdict (only if chosen) | job_work\service.py `record_qc_result` L2316-2450 | `change_stock_state(QC_HOLD→AVAILABLE/DAMAGED, ...)`; verdict must cover the whole hold (`assert_verdict_covers_hold` L2284-2313) |
| Capability flag enforcement | sales\service.py L728-740 | `BusinessRuleError(f"Line {n}: {code} is not flagged as returnable.", details=...)` |
| Router | procurement\router.py L1155-1237 | Thin: one service call, then `ApiResponse[...].ok(obj, "Sales return X raised")`; `PaginatedResponse`; `require_permission(Permission.X)` on every route; `status_code=201` on create; literal paths before `/{id}` |
| Schemas | procurement\schemas.py L1080-1181 | `ConfigDict(extra="forbid")` on inputs; `from_attributes=True` on reads; `PositiveQuantity`, `Reason` types; `model_validator` for duplicate lines |
| Repository | procurement\repository.py L1232-1328 | `_scoped()`; `lock_for_update`; `sortable_fields` whitelist; `list_page(params, stmt=..., order_by=sort.resolve(fields, default))`; an empty id list returns `{}` |
| Permission grant migration | `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\20260906_0902_grant_item_attribute_permissions.py` | Literal strings; `array_append` only for `is_system` roles SUPER_ADMIN and ADMIN; reversible |
| Additive column migration | `...\20260905_0000_purchase_return_line_stock_state.py` | `ADD COLUMN ... NOT NULL server_default` (catalogue-only change); review notes on locking, data and rollback |
| Frontend model, page and dialogs | `apps\web\src\features\procurement\purchase-return-model.ts`, `purchase-returns-page.tsx`, `purchase-return-dialogs.tsx` | `SchemaOf<'X'>` types; query keys under a registry prefix; status predicates that mirror the machine; decimal.js; `success.*` from `lib/messages.ts` L23-51; invalidate `inventory.all` after posting |
| Frontend test | `purchase-returns-page.test.tsx` | `renderScreen` / `RouteTable` / `ok` / `fail` / `page` harness; assert permission gating, bodyless post, server-error remapping |
| Integration test | `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_dispatch_service.py` (module-scoped private schema, L95+) plus `tests\seed_support.py` (`seed_item`, `seed_warehouse`, `seed_customer`, `seed_serial`) | Real allocation and inventory engines; seed through the ORM, never raw INSERT |

---

## Integration points

1. **`inventory.service.post_movements`**: `CUSTOMER_RETURN`, magnitude quantities, `reference_type="sales_return"` (matches the `STAGE_BY_REFERENCE_TYPE` key), `reference_id` and `reference_code`.
2. **The DISPATCH ledger rows of the source dispatch** are the source of truth for batch and cost:
   - `inventory_transactions` filtered by `reference_type="dispatch"`, `reference_id=dispatch.id`, `transaction_type="DISPATCH"`, read in one indexed query.
   - Per serial: the latest DISPATCH row with that `serial_id` (index `ix_inventory_transactions_serial`), ordered by `(created_at, id)`.
   - Optionally the exact layers via `CostConsumptionRepository.by_transaction`.
3. **`dispatch_lines`** (dispatch's table): new `returned_quantity`, read-modify-write under a row lock.
4. **`stock_serials`**: handled by the engine (`RETURNED`, relocated to the return warehouse). A returned unit only becomes `IN_STOCK` again through a `QC_RELEASE` that lands in AVAILABLE and **names the serial**.
5. **Quality port**: `create_in_process_inspection(..., reference_type="sales_return", vendor_id=None, batch_id=...)`, then read back with `inspections_for_reference`.
6. **Finance credit-note port**: `post_customer_adjustment(..., CustomerAdjustmentCreate(adjustment_type=CREDIT, amount, account_id, entry_date, reason))`. That call also moves `customers.outstanding_amount` in the same transaction.
   - Needs a contra account (none provisioned; add `SystemAccount.SALES_RETURNS` to `DEFAULT_CHART` or take `account_id`).
   - `GstRepository.output_rows_stmt` needs a credit-note reversal leg.
7. **`sales_order_lines`**: leave `dispatched_quantity` alone. MRP open demand (`mrp\repository.py` L161-166), the `dispatched_within_ordered` CHECK, the open-item partial index and the GST share all read it. Optionally add `returned_quantity` for reporting.
8. **Notifications**: `notify_role(... NotificationType.QC_INSPECTION_PENDING ...)` (enum at notifications\models.py L120; no DB CHECK on the type).
9. **Audit**: `record_audit` and `record_status_change` in the same transaction. Add `'sales_return'` to frontend `audit-types.ts`.
10. **Permissions** (`C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`):
    - new `SALES_RETURN_VIEW` and `SALES_RETURN_CREATE` (next to L116-117);
    - add `"sales_return"` to `PERMISSION_GROUPS["Sales"]` (L218);
    - add to the `_SALES`, `_DISPATCH`, `_STORE` and `_QC` (view) presets (L263-420);
    - a grant migration;
    - `lib/permissions.ts`.
11. **Reports**: `reports\repository.py` `CONSUMPTION_TYPES` (L136-141) counts DISPATCH as movement and does not net returns. Decide whether fast/slow-moving should subtract `CUSTOMER_RETURN`.
12. **Router registry**: `C:\Project\Other\ATCRAFT\apps\api\src\api\router.py` `MODULE_ORDER` (L22-48). Models are auto-discovered by `src\core\registry.py`.

---

## Traps and constraints

- **The RETURN pool is not segregated.** `STATE_BUCKETS[RETURN] = None`, so returned goods posted as `RETURN` into the finished-goods warehouse are immediately reservable and dispatchable. Use `QC_HOLD` or `DAMAGED`, or a separate warehouse.
- **The QC decision grain has no bin.** `_post_decision_movements` posts without `location_id`, and `QcInspection` has no location. Held stock must sit at `location_id=None` or the decision fails with "Only 0 of X is in qc hold".
  - The GRN path appears to carry this latent defect today. `GoodsReceiptLine.location_id` with `requires_qc` posts QC_HOLD at the bin (procurement\service.py L2861, L3192), and nothing guards against it. Found by reading the code only; not run or tested. Do not copy it.
- **Serial-tracked goods under QC:**
  - Quality cannot name units, so accepted units stay `RETURNED`.
  - Dispatch (L803-808) and packing (L827-831) refuse any status but `IN_STOCK`.
  - A quality SCRAP decision is refused (SERIAL_REQUIRED).
  - No existing QC-hold path carries serials. GRN refuses them (§20), job work refuses them (L812-815), and assembly refuses serial-tracked output.
- **Serial-tracked items cannot move on a stock transfer** (§25, `_assert_lines_are_traceable`). A unit returned into WH-RET can only ship again from WH-RET.
- **No exemption for arrivals.** The engine refuses arrivals of inactive items or into inactive warehouses or retired bins (§23 gates). Quality `_prepare` also refuses an inactive item. Check up front with a clear message.
- **Batch and cost come from the ledger, not the dispatch line.**
  - Reserved dispatch lines have `inventory_transaction_id = NULL`.
  - `allocation.consume` posts at the reservation's grain, and sales reservations carry no batch.
  - Read batch and cost from the DISPATCH rows; for serial lines `inventory_transaction_id` holds only the first row.
- **Standard-cost fallback breaks the round trip.** Without `unit_cost` the return is booked at standard cost, so the company's value changes on a round trip. Pass the dispatch cost.
  - FIFO is per position: returns held at the finished-goods grain share its layers, and a later SCRAP consumes the *oldest* layer. A dedicated return warehouse keeps return layers separate.
- **Do not reintroduce these known traps:**
  - order ledger and audit reads by `(created_at, id)`;
  - an empty `in_()` list must mean "nothing";
  - `sort.resolve` style, never `clause or default`;
  - no `session.rollback()` inside a service;
  - in tests, hold ids in plain variables after an expected failure;
  - `model_validate` on a model with a `lazy="raise"` `lines` relationship 500s list endpoints, so follow `return_detail()` or `_declare_lines_empty_if_unloaded` (inventory\service.py L3820-3837), and test a list that contains a row you created;
  - Python-side `onupdate` only;
  - CHECK names of 63 bytes or less once `ck_<table>_` is prepended (`test_schema_identifier_length.py`).
- **Two release paths.** Job work has both `decide_inspection` and `record_qc_result` for the same hold. Pick one owner per hold.
- **Vendor rating pollution.** Passing a `vendor_id` on a return inspection would feed vendor ratings (`vendors.service.load_performance`) and trigger "Vendor return owed". Pass `None`.
- **Credit-note privilege.** Posting a credit note from the return must require `payment.approve`, the same right `POST /finance/customer-adjustments` needs. Otherwise it is a back door to issuing credit.
- **GST.** Output tax is recognised at dispatch with no return leg, so it will be overstated until a reversal is added.
- **Route and import placement.**
  - Literal routes must be declared before `/{id}`. Under `/sales` a `/returns` route would be captured by `/{order_id}` (see the procurement\router.py L7-11 note).
  - `dispatch.service` imports `sales.service`, so `sales.service` must not import `dispatch.service`.
- **Meta-tests that will enforce rules on new code:**
  - `test_status_badge_catalogue.py`: every `*Status` / `*State` member needs a badge.
  - `test_state_machine_entity_names.py`.
  - `test_frontend_permission_gates.py` and `test_frontend_permission_catalogue.py`.
  - `test_role_presets_reach_roles.py`: the new resource must be in a permission group.
  - `test_users_permissions.py`.
  - `test_frontend_contract_types.py`: use `SchemaOf`, or register any hand-written type.
  - `test_e2e_sweep_routes.py`: the new route must appear in `browser-sweep.mjs`, `a11y-sweep.mjs`, `responsive-sweep.mjs` and `permission-sweep.mjs`.
  - `test_postman_and_contract_targets.py`: `docs/api.md` L28 says "494 requests in 27 folders" and must be updated.
  - `test_frontend_constants.py`: no inline limits or debounces.
  - `test_schema_identifier_length.py`.
- **Front-end behaviour rules:** status predicates must mirror the machine, and a column must not be sortable unless the repository's `sortable_fields` allows it.

---

## Files a change here will touch

**Backend: new module** (recommended `C:\Project\Other\ATCRAFT\apps\api\src\modules\sales_returns\`)
- `__init__.py`, `models.py`, `repository.py`, `schemas.py`, `service.py` (optionally `service_rules.py` for pure rules), `router.py`.

**Backend: edits**
- `C:\Project\Other\ATCRAFT\apps\api\src\api\router.py`: add to `MODULE_ORDER` after `"dispatch"`.
- `C:\Project\Other\ATCRAFT\apps\api\src\common\codes.py`: `DocTypes.SALES_RETURN`.
- `C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`: enum, group, presets.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\dispatch\models.py`: `DispatchLine.returned_quantity` plus CHECKs.
- `dispatch\schemas.py` and `dispatch\repository.py`: `returned_quantity` on `DispatchLineRead`, and a `left_only` filter for the picker.
- Optionally `dispatch\service.py`: set `inventory_transaction_id` for reserved lines.
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\finance\models.py` and `service.py`: `SystemAccount.SALES_RETURNS` in `DEFAULT_CHART`.
- `finance\repository.py`: GST reversal leg.
- Optionally `C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\repository.py` (`CONSUMPTION_TYPES` netting).
- Optionally `C:\Project\Other\ATCRAFT\apps\api\src\modules\sales\models.py` (`returned_quantity`).
- Optionally `C:\Project\Other\ATCRAFT\apps\api\src\core\demo_data\operations.py`: one demo return.

**Migrations** (head is `2f5e08c7ba61`, `20260906_0902_grant_item_attribute_permissions.py`)
- `...\migrations\versions\2026xxxx_sales_returns.py`: two tables plus `dispatch_lines.returned_quantity`.
- `...\2026xxxx_grant_sales_return_permissions.py`.

**Tests**
- New unit tests under `C:\Project\Other\ATCRAFT\apps\api\tests\unit\`: `test_sales_return_lifecycle.py`, `test_sales_return_rules.py`.
- New integration test `...\tests\integration\test_sales_return_service.py`.
- Optional API test under `...\tests\api\`.

**Frontend**
- New folder `C:\Project\Other\ATCRAFT\apps\web\src\features\sales-returns\`: `sales-return-model.ts`, `sales-returns-page.tsx`, `sales-return-dialogs.tsx`, `sales-returns-page.test.tsx`.
- Edits:
  - `app\router.tsx`, `app\route-loaders.ts`, `app\navigation.ts`;
  - `lib\permissions.ts`;
  - `lib\query-client.ts` (optional `salesReturns` keys);
  - `features\audit\audit-types.ts`;
  - `features\dispatch\dispatch-page.tsx` (a "Raise return" action on dispatched SALES rows, gated on `P.SALES_RETURN_CREATE`);
  - `components\shared.tsx` only if a new status word is introduced;
  - regenerated `types\openapi.ts`.

**Contract and docs**
- `make contract` regenerates `C:\Project\Other\ATCRAFT\openapi.json` and `C:\Project\Other\ATCRAFT\postman_collection.json`.
- `C:\Project\Other\ATCRAFT\docs\api.md`: L28 request and folder counts.
- `C:\Project\Other\ATCRAFT\docs\database.md`: §2 module map, §7 state machines, §8 integration table.
- `C:\Project\Other\ATCRAFT\docs\assumptions.md`: new sections (see the Assumptions part of the recommendation).
- `C:\Project\Other\ATCRAFT\docs\handbook\content.json`.

**e2e** (`C:\Project\Other\ATCRAFT\tests\e2e\scripts\`)
- `a11y-sweep.mjs`, `browser-sweep.mjs`, `responsive-sweep.mjs`, `permission-sweep.mjs`, `data-sweep.mjs`, `tenant-isolation.mjs`, `capture-screens.mjs`.
- `tests\e2e\specs\ui-write-paths.spec.ts`: one journey.

---

## Recommendation: the Sales Return document

**Placement.** Create a new module `sales_returns` with router prefix `/sales-returns` (tag "Sales Returns", which adds one Postman folder) and a frontend route `/sales-returns`. This avoids the `/sales/{order_id}` route-capture trap and the `sales.service` ↔ `dispatch.service` import cycle. The alternative is to put it inside `dispatch`, which mirrors procurement keeping the GRN and its return together.

**What a return references.** Each line references a `dispatch_line_id` (NOT NULL). That row carries the item, batch, serials, sales-order line and the ledger anchor. The header references `dispatch_id` (NOT NULL), which gives the customer and the origin warehouse. A sales-order line alone is not enough: it can be split across consignments with different lots and costs, and it carries no serials.

### Tables

**`sales_returns`** (`TenantEntity`)
- `code` String(64), with `uq (company_id, code)`.
- `dispatch_id` FK RESTRICT NOT NULL.
- `customer_id` FK RESTRICT NOT NULL, copied from the dispatch.
- `sales_order_id` FK NULL.
- `warehouse_id` FK RESTRICT NOT NULL: the receiving store.
- `return_date` Date.
- `status` String(24), with CHECK.
- `reason` String(255) NOT NULL.
- `return_reason_code` String(24) with CHECK, from a closed vocabulary `DEFECTIVE | DAMAGED_IN_TRANSIT | WRONG_ITEM | EXCESS | REFUSED_AT_DELIVERY` (a `*Reason` enum, not a `*Status`, so the badge guard does not apply).
- `requires_qc` Boolean, default true.
- `currency`, `subtotal`, `tax_amount`, `total_amount` (Money).
- `credit_note_code` String(64) NULL and `credit_journal_id` GUID NULL (bare UUID, because finance owns the journal), `credited_at`.
- `received_at`, `received_by_id`, `closed_at`, `cancellation_reason`, `notes`.
- CHECKs:
  - `status NOT IN ('RECEIVED','CLOSED') OR (received_at IS NOT NULL AND received_by_id IS NOT NULL)`;
  - `status <> 'CANCELLED' OR cancellation_reason IS NOT NULL`;
  - money totals ≥ 0.
- Indexes: `(company_id, dispatch_id)`, `(company_id, customer_id, return_date)`, `(company_id, status, return_date)`.

**`sales_return_lines`**
- `sales_return_id` FK CASCADE, `line_number`, with `uq (sales_return_id, line_number)`.
- `dispatch_line_id` FK RESTRICT NOT NULL.
- `sales_order_line_id` FK NULL, `item_id`, `description`, `quantity` > 0, `uom_id`.
- `batch_id` FK stock_batches.
- `serial_ids` uuid[], with GIN index and CHECK cardinality equals quantity (or 0).
- `unit_cost` UnitPrice: the dispatch cost the movement is booked at.
- Credit pricing, apportioned from the sales-order line by quantity share exactly as `GstRepository.output_rows_stmt` does: `unit_price`, `discount_amount`, `tax_rate`, `cgst_amount`, `sgst_amount`, `igst_amount`, `tax_amount`, `line_total`.
- `stock_state` String(24), with CHECK `IN ('QC_HOLD','DAMAGED','RETURN')`.
- `qc_inspection_id` GUID NULL (bare), `inventory_transaction_id` GUID NULL (anchor), `reason`, `notes`.
- Index `(company_id, dispatch_line_id)`.

**`dispatch_lines.returned_quantity`**: Quantity, default 0, with CHECKs `>= 0` and `<= quantity`. Only return posting writes it.

### State machine

`SALES_RETURN_LIFECYCLE`, entity `"Sales return"`:
- `DRAFT → RECEIVED`: `sales_return.create`, label "Receive", irreversible. This edge moves stock.
- `DRAFT → CANCELLED`: `sales_return.create`.
- `RECEIVED → CLOSED`: `sales_return.create`, label "Close". Refused while any line's inspection still holds stock (`still_held > 0`) and, for serial lines, while any unit is still undisposed.
- Terminal: CLOSED and CANCELLED. Every status word already has a badge colour.
- The credit note is an action recorded on the header, not a status. It is allowed in RECEIVED or CLOSED and requires `payment.approve`.

### Posting (receive)

All in one `transaction(session)`:
1. Lock the header and assert DRAFT→RECEIVED.
2. Load the dispatch:
   - type must be SALES;
   - status must be in `IN_FLIGHT_STATUSES + DELIVERED_STATUSES` (the same set as finance's `TAXABLE_DISPATCH_STATUSES`; that includes refused-at-delivery).
3. Lock the dispatch lines by primary key, then re-check `quantity - returned_quantity` under the lock.
4. In one query, read the DISPATCH ledger rows for this dispatch to resolve each line's batch, and each serial's last DISPATCH row. Check that each serial is in `dispatch_line.serial_ids`, its status is DISPATCHED, and its last DISPATCH row points at this dispatch.
5. Build the `CUSTOMER_RETURN` movements:
   - `warehouse_id=return.warehouse_id`;
   - `location_id=None` whenever QC applies;
   - `batch_id` from the ledger;
   - one movement per serial;
   - `unit_cost` = blended dispatch cost, `sum(|total_cost|) / sum(|quantity|)` for that line's rows, or the serial's own row;
   - `stock_state=QC_HOLD` if `requires_qc`, otherwise DAMAGED (visibly broken) or RETURN (explicit, documented as available);
   - `reference_type="sales_return"`;
   - `occurred_at=utcnow()`.
6. Call `post_movements`, then add to `dispatch_line.returned_quantity`.
7. Raise inspections through the quality port (non-serial lines only; see the QC section).
8. Write posting evidence, call `_transition`, write `record_audit(INVENTORY_MOVEMENT)`, and notify the QC roles.

**Default receiving warehouse.** Default to the company's active RETURN-type warehouse (seeded `WH-RET`) and let the user override it. For serial-tracked lines, default to `dispatch.warehouse_id`, because serials cannot be transferred (§25).

### QC hookup

**Non-serial lines.**
- Receive into QC_HOLD, then call `create_in_process_inspection(item_id, quantity, warehouse_id, reference_type="sales_return", reference_id, reference_code, vendor_id=None, batch_id)`. The stage resolves to RETURN automatically.
- Quality's existing decision moves QC_HOLD to AVAILABLE, DAMAGED or SCRAP.
- The return's detail and Close step read the verdict with `inspections_for_reference` (pull model). There is no second release path.

**Serial lines.**
- Receive into QC_HOLD with no quality inspection.
- Add a return-owned `POST /sales-returns/{id}/units` that takes accepted, rejected and scrapped `serial_ids`. It posts per unit:
  - `change_stock_state(QC_HOLD→AVAILABLE, serial_id)`: a QC_RELEASE that turns the unit back to `IN_STOCK`;
  - `→DAMAGED`: the unit stays RETURNED;
  - `→SCRAP`: RETURNED becomes SCRAPPED.
- This mirrors job work's `record_qc_result` but per unit.
- If scope must be cut, refuse serial-tracked lines in v1, the same refusal GRN (§20) and transfers (§25) make.

### Credit-note hookup

`POST /sales-returns/{id}/credit-note`, gated on `payment.approve`:
- It calls finance through a `resolve_credit_note_port()`, defaulting to `post_customer_adjustment` with `adjustment_type=CREDIT`, `amount=total_amount`, `account_id` = the new `SystemAccount.SALES_RETURNS` contra, and `reason=f"Sales return {code}"`.
- It stores `credit_note_code` and `credit_journal_id`, and refuses a second credit.
- Add the output-GST reversal to `GstRepository`.
- When the sales invoice module lands, point the port at its credit-note function.

### Validations

**Create**
- The dispatch exists in the tenant, is SALES type, and is in a returnable status.
- Each line belongs to that dispatch and appears at most once (schema validator).
- `quantity ≤ dispatch_line.quantity - returned_quantity`.
- The item has `can_return` (first enforcement of the flag) and is active.
- Serial lines: serials required, count equals quantity, each one a subset of the line's `serial_ids` and currently DISPATCHED.
- Non-serial lines carry no serials.
- Batch is resolvable.
- The warehouse exists, is active and passes `assert_warehouse_scope`.
- At most `MAX_LINES_PER_DISPATCH` (200) lines.
- A blocked customer may still return goods (ASSUMPTION).
- Vendor principals are blocked with `forbid_vendor_users`.

**Post**: everything is re-checked under the locks.

### Assumptions to record in `docs/assumptions.md`
1. Valuation at the original dispatch cost.
2. Choice of receiving warehouse and pool.
3. Serial disposition owned by the return.
4. Credit value apportioned from the sales-order line; GST reversal.
5. `dispatched_quantity` is never decremented, and replacement goods need a new sales-order line.

### Tests to mirror

**Unit**
- Lifecycle shape, following `test_procurement_lifecycle.py` / `test_dispatch_lifecycle.py`: `machine.validate()`, terminal states, edge permissions, irreversible flag.
- Pure returnable arithmetic and serial-set rules, following `test_procurement_return_pools.py`.
- Blended-cost helper.

**Integration** (module-scoped schema like `test_dispatch_service.py`; seed through `seed_support`)
- Dispatch then receive posts `+qty` `CUSTOMER_RETURN` with `reference_type="sales_return"`, and on_hand and qc_hold rise at the return warehouse.
- A return cannot exceed what was dispatched, and two returns cannot exceed it between them.
- NOT_DISPATCHED, CANCELLED and non-SALES dispatches are refused.
- Round trip is value-neutral: FIFO receipts 10 @ 100 and 10 @ 120, dispatch 15, return 5, and the returned layer is priced at the dispatch blend.
- A batch-tracked return re-enters the same lot.
- Serial: a unit becomes RETURNED and is relocated; a second return of the same unit is refused; a serial not on the line is refused; after "accept unit" it is IN_STOCK and can be dispatched again.
- The QC inspection has stage RETURN and reference `sales_return`. ACCEPT moves stock to available and REJECT to damaged, at the bin-less grain.
- `can_return=False` and inactive items are refused with clear messages.
- The list endpoint lists a return the test created.
- Tenant isolation.
- Audit trail ordered by `(created_at, id)`.
- The credit note writes a receivable CREDIT and lowers the customer's outstanding amount.

**Frontend**: page and dialog tests in the shape of `purchase-returns-page.test.tsx`, plus an e2e journey in `ui-write-paths.spec.ts`.
