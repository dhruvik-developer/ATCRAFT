## Verdict on the audit claim (gap 11)

The audit is right, with a few nuances.

- **Shift is free text in two places, not one.** It is a `String(16)` column on `work_orders.shift` and on `production_entries.shift`. It also passes through `POST /production/{id}/complete` (`CompleteRequest.shift`).
- **What is absent:**
  - No shift master.
  - No roster or plan.
  - No shift calendar or holiday calendar.
  - No attendance.
  - No report that groups by shift or operator.
  - No OEE calculation.
  - No company-wide list of production bookings.
- **Neighbouring pieces that do exist:**
  - `work_centers.working_hours_per_day`, which is the only "calendar" in the system.
  - A machine availability state machine.
  - Operator (a `users` row) and machine assignment on each work order.
  - Operator, machine and downtime on each booking.
  - Two WIP endpoints.
  - Machine-utilisation and production-efficiency reports.
- **The proposal PDF could not be checked.** `Atcraft Manufacturing ERP Development_compressed.pdf` in the repo root is image-only: `pdftotext` returns 0 lines and poppler is not installed. So I could not compare the proposal's own wording for the roster.

Absolute root for every path below: `C:\Project\Other\ATCRAFT`. Each file is named in full in its own heading. `Lnnn` means a line number in that file.

---

## Exists today

### A. Production order planning fields
**`C:\Project\Other\ATCRAFT\apps\api\src\modules\production\models.py`**
- L106-121 `ProductionOrderStatus`: DRAFT → APPROVED → RELEASED → MATERIAL_ISSUED → IN_PROCESS → COMPLETED → CLOSED, plus CANCELLED.
- L204-361 `class ProductionOrder(TenantEntity, OptimisticLockMixin)`:
  - `code` String(32); `item_id`, `bom_id`, `routing_id`.
  - `planned/produced/rejected/scrap_quantity`; `warehouse_id`; `plant_id` (nullable).
  - **`planned_start_date`, `planned_end_date: Date`** (L250-251). The comment at L248-249 says *"Planning is done in whole days; the shop floor's minute-level detail lives on the work orders, which is where a scheduler actually needs it."*
  - `actual_start_date` / `actual_end_date` (Date); `priority` int, default 100, lower is more urgent.
  - `status`; `source_type` / `source_id`; approval pair; cost columns.
  - Partial index `ix_production_orders_open` (L322-328).
- There is no shift or calendar field on the order.

### B. Work orders (one row per routing operation)

**Model.** `production/models.py` L137-141 `WorkOrderStatus` = PENDING / IN_PROGRESS / COMPLETED / CANCELLED. L459-587 `class WorkOrder(TenantEntity)`:

| Field | Type / rule |
|---|---|
| `code` | String(32), from `next_document_number(DocTypes.WORK_ORDER)` ("WO") |
| `production_order_id`, `sequence` | FK CASCADE; `uq (production_order_id, sequence)` |
| `routing_operation_id`, `operation_code`, `operation_name` | **snapshot** of the routing step. No FK on purpose (L473-476) |
| `work_center_id` | FK `work_centers` RESTRICT, NOT NULL |
| `machine_id` | nullable. Composite FK `fk_work_orders_machine_id_work_center_id_machines` (`machine_id`, `work_center_id`) → `machines(id, work_center_id)` (L551-556) |
| `operator_user_id` | FK `users.id` ON DELETE SET NULL |
| **`shift`** | **String(16), nullable, free text** (L490-492) |
| `planned_start_at`, `planned_end_at`, `actual_start_at`, `actual_end_at` | TIMESTAMPTZ, nullable |
| `planned_quantity`, `completed_quantity`, `rejected_quantity` | Quantity |
| `planned_setup_minutes`, `planned_run_minutes`, `process_cost`, `work_center_cost_per_hour`, `work_center_setup_cost` | snapshots taken at release |
| `status`, `notes` | |

Indexes (L557-563): code; (order, sequence); `ix_work_orders_work_center`; `ix_work_orders_order`; `ix_work_orders_operator`; `ix_work_orders_machine`. **No index on shift and no filter on shift.** CHECKs at L564-586 include `completed_operations_were_started`.

The full comment above the shift column (L490-492), as requested:
```python
    #: Free text ("A", "GENERAL", "NIGHT"). A shift master would be an extra
    #: table for a field no rule branches on.
    shift: Mapped[str | None] = mapped_column(String(16), nullable=True)
```
- That two-line comment is the whole of it.
- `Machine.machine_type` makes the same argument (`routing/models.py` L185-187).
- The item-attribute migration explicitly overturned exactly this argument: *"Nothing in the application branched on them - which is exactly why they can become a client-editable master."*
- `ProductionEntry.shift` (L758) has no comment at all.

**Schemas** — `C:\Project\Other\ATCRAFT\apps\api\src\modules\production\schemas.py`
- L41 `ShiftField = Annotated[str, Field(min_length=1, max_length=16)]`. Only `extra="forbid"` applies: no trimming and no upper-casing, so "A", "a" and "A " are three different shifts.
- L160-187 `WorkOrderRead` (includes `shift`).
- L587-597 `WorkOrderUpdate`: `operator_user_id`, `machine_id`, `shift: ShiftField|None`, `planned_start_at`, `planned_end_at`, `notes`.
- L600-606 `WorkOrderTransitionRequest`: `status`, `occurred_at`, `reason`.
- L609-617 `WorkOrderFilters`: order, status, work_center_id, machine_id, operator_user_id, open_only. **No shift filter and no date window.**

**Service** — `C:\Project\Other\ATCRAFT\apps\api\src\modules\production\service.py`
- L284-315 `WORK_ORDER_FLOW`: PENDING → IN_PROGRESS | CANCELLED; IN_PROGRESS → COMPLETED (irreversible) | CANCELLED. Every edge needs `WORK_ORDER_MANAGE`.
- L605-647 `plan_work_orders(operations, *, planned_quantity, start_at) -> list[WorkOrderPlan]`. ASSUMPTION: operations run strictly back to back, 24/7, with no shift windows and no breaks.
- L1676-1738 `_create_work_orders(session, tenant, order, routing)`, called from `release_order` (L1525):
  - `start_at = datetime.combine(order.planned_start_date, datetime.min.time(), tzinfo=UTC)` (L1698), i.e. 00:00 UTC.
  - `machine_id` is copied from the routing operation.
  - **Operator and shift are never set at release.**
- L3093 `list_work_orders(session, tenant, filters, params, sort)`.
- L3106-3133 `_assert_order_open_for_shop_floor`: locks the order first.
- L3136-3205 `update_work_order(session, tenant, work_order_id, payload: WorkOrderUpdate) -> WorkOrder`:
  - Refuses COMPLETED and CANCELLED work orders.
  - Calls `_assert_operator` / `_assert_machine(work_center_id=wo.work_center_id)` only when those keys are sent.
  - Then runs a **blind `setattr` loop** (L3179-3180).
  - Checks the planned window and writes an audit row (UPDATE, old/new).
- L3208-3255 `transition_work_order(...)`: stamps `actual_start_at` / `actual_end_at` from `occurred_at or utcnow()`, then calls `_transition`, which records the status change.

**Routes** — `C:\Project\Other\ATCRAFT\apps\api\src\modules\production\router.py`
- `GET /production/work-orders` L368-386 (`WORK_ORDER_VIEW`). Sortable fields: `sequence`, `status`, `planned_start_at`, `created_at` (`repository.py` L801-806).
- `PATCH /production/work-orders/{work_order_id}` L389-408 (`WORK_ORDER_MANAGE`).
- `POST /production/work-orders/{work_order_id}/status` L411-427 (`WORK_ORDER_MANAGE`).
- `GET /production/{order_id}/work-orders` L801-817.
- `POST /production/{order_id}/start` L577-593 (`WORK_ORDER_MANAGE`).
- The module docstring (L29-31) requires literal routes to be declared before `/{order_id}`.

### C. Production entries (shop-floor bookings)

**Model.** `production/models.py` L736-811 `class ProductionEntry(TenantEntity)`. There is no update path ("Never edited after it is written").
- `production_order_id`; `work_order_id` (nullable).
- **`entry_date: Date`**. L755-757: *"Business date of the shift, which is regularly not the date the entry was keyed - night shift output is booked the following morning."*
- **`shift` String(16), nullable** (L758).
- `operator_user_id` → users, ON DELETE SET NULL.
- **`machine_id` → `machines.id` is a plain FK**, not the composite work-centre FK (L763-765).
- `produced_quantity`, `rejected_quantity`, `rejection_reason`.
- `downtime_minutes`. L774-776: *"Reported for OEE"*. No OEE calculation exists anywhere.
- `notes`; `output_transaction_id`.
- Index `ix_production_entries_date (company_id, entry_date, shift)` (L791), commented *"The daily production report, and the shift board."* **Neither of those exists.**
- CHECKs: `entry_records_something`, `rejections_carry_a_reason`.

**Schemas** (`schemas.py`)
- `ProductionEntryCreate` L510-539.
- `ProductionEntryRead` L542-558.
- `ProductionEntryFilters` L569-579, including `shift: str|None` (max 16). The match is exact and case-sensitive (`repository.py` L1105-1106).
- `CompleteRequest` L243-276 carries `shift`, `operator_user_id`, `machine_id`, `work_order_id`, `entry_date`.

**Service** (`service.py`)
- `_book_entry(session, tenant, order, payload, *, force_output=False)` L2869-2992:
  - Validates the operator and the machine. The machine-in-centre check runs only when a work order is named (L2895-2901).
  - Books output when the booking is at the last operation or names no work order (L2908-2909).
  - Writes the entry with `entry_date=payload.entry_date or today()` (L2922). `today()` is the **UTC** date (`src/core/clock.py`).
  - The first booking on a PENDING work order starts it.
  - The audit row includes `shift` (L2983).
- `_post_output_receipt` L2995-3039: the receipt's `occurred_at` is midnight UTC on `entry_date` (L3034).
- `create_production_entry` L3042-3072: accepts only MATERIAL_ISSUED and IN_PROCESS orders.
- `complete_order` L1765-1868: builds `ProductionEntryCreate(... shift=payload.shift ...)` (L1794-1804).
- `list_production_entries` L3075-3085.

**Routes**
- `POST /production/{order_id}/entries` L756-777 (`PRODUCTION_ENTRY_CREATE`).
- `GET /production/{order_id}/entries` L780-798 (`PRODUCTION_ORDER_VIEW`). The path overrides `production_order_id`.
- **There is no company-wide entries endpoint**, even though `ProductionEntryFilters` already supports operator, machine, shift and date filters.

### D. How operators are represented, and how machine/operator allocation works
- **Operators are `users` rows.** There is no operators or employees table.
  - **`C:\Project\Other\ATCRAFT\apps\api\src\modules\users\models.py`** L99-185 `User(TenantMasterEntity)`.
  - `email` is CIText NOT NULL; `employee_code` is String(32), nullable, not unique (L113); `is_active` (L122); `plant_id`, `department_id` (L142-147).
  - `vendor_id` (L150-152): vendor-portal users share this table.
  - So every operator has to be a login account with an email address.
- **`OperatorLookupRepository.exist_in_tenant`** (`C:\Project\Other\ATCRAFT\apps\api\src\modules\production\repository.py` L311-325) checks only `company_id` and `deleted_at IS NULL`. As a result `_assert_operator` (service L959-965) **accepts inactive users and vendor-portal users**.
- **`_assert_machine`** (service L968-990) uses `MachineRepository.load` (`routing/repository.py` L381-405), which filters only on company and `deleted_at`. **Machine status (BREAKDOWN, MAINTENANCE, or RETIRED set through `change_status`) and `is_active` are not checked.** The only rule is "same work centre as the work order".
- The allocation flow today:
  1. Release copies the routing's machine.
  2. A supervisor PATCHes the operator, machine, shift and planned window.
  3. The operator is started and completed through `/status`.
- There are **no capacity or double-booking checks of any kind**. One operator or one machine can sit on any number of overlapping work orders.
- **Permissions gap.** **`C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`**:
  - The `_PRODUCTION` preset (L326-360), used by PRODUCTION_SUPERVISOR (L513-518) and PLANT_MANAGER (L473-487), **does not contain `user.view`**.
  - Every operator picker in the UI calls `GET /users`, which needs `user.view`.
  - So supervisors see an empty picker or "Not available to your account", and the queue shows "Assigned" with no name.

### E. WIP board
- `GET /production/wip` (router L239-254, `PRODUCTION_ORDER_VIEW`) → `wip_orders` (service L3263-3292) → `ProductionOrderRepository._wip_stmt` / `wip_page` (repository L507-575 and onward). **No frontend screen uses it.**
- `GET /production/wip/work-centres` (L257-273, `WORK_ORDER_VIEW`) → `wip_work_centres` (L3295-3318) → `WorkOrderRepository.load_by_work_centre` (L888-973). It reports remaining minutes, uses `MAX_WORK_CENTRE_ROWS=200`, and ignores `late_only`. The frontend is `wip-board-panel.tsx`.
- Neither endpoint has a shift or calendar dimension.

### F. Routing: work centres, machines, operations

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\routing\models.py`**
- L60-67: `DEFAULT_WORKING_HOURS_PER_DAY = Decimal("8")` (*"Fallback shift length … a site running two shifts changes it here"*) and `MAX_WORKING_HOURS_PER_DAY = 24`.
- L70-81 `MachineStatus`: AVAILABLE / RUNNING / MAINTENANCE / BREAKDOWN / RETIRED.
- L97-165 `WorkCenter(TenantMasterEntity)`:
  - `code`, `name`, `plant_id`, `department_id`.
  - `capacity_per_hour`: stored but **used by no calculation**.
  - `cost_per_hour`, `setup_cost`.
  - **`working_hours_per_day`** (L144-149, CHECK 0 < h ≤ 24).
  - `is_active`.
- L168-236 `Machine(TenantMasterEntity)`: code, name, `work_center_id`, free-text `machine_type`, `capacity_per_hour`, `cost_per_hour`, `status`, last/next maintenance dates, `is_active`. `uq_machines_id_work_center_id` (L216) is the target of the composite FK. **Machines have no hours and no calendar.**
- L329-428 `RoutingOperation`: `setup_time_minutes`, `run_time_per_unit_minutes`, `queue_time_minutes`, `move_time_minutes`, `process_cost`, composite machine FK (L403-408).

**`C:\Project\Other\ATCRAFT\apps\api\src\modules\routing\service.py`**
- L162-216 `MasterLifecycleState` and `WORK_CENTER_LIFECYCLE`. The state is derived from `is_active` and `deleted_at`.
- L219-294 `MACHINE_AVAILABILITY`. It is not linked to work orders.
- L386-418: `calculate_operation_time` and `chargeable_operation_time`.
- L463-476 `resolve_working_hours_per_day`. ASSUMPTION: use the shortest working day among the centres on the routing.
- L479-506 `total_lead_time_days`: rounded **up** to whole days.
- L576-751 `WorkCenterService`: list / get / create / update / set_active / retire.
- L766-970 `MachineService`. `change_status` to RETIRED clears `is_active` but **does not soft-delete** (L876-879).
- L1587-1600 `manufacture_lead_time_days`: the seam MRP calls.

**Repository and router**
- `routing/repository.py` L59-62 `WORKABLE_MACHINE_STATUSES` (documented as "warn on approval rather than block").
- `routing/router.py`: prefix `/routing`, tag "Routing & Work Centers". Literal routes come before `/{routing_id}` (docstring L7-11). Work centres L149-266; machines L274-363.

### G. Reports that use shift, operator or machine data
**`C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\`**
- `router.py`:
  - L529-554 `GET /reports/production-efficiency`.
  - L557-580 `GET /reports/machine-utilisation`. Its description says *"the system holds no shift calendar, so no weekend or holiday assumption is made."*
  - Both are gated on `REPORT_VIEW` and answer 200 or 202.
- `repository.py`:
  - L151-157 `_WORK_ORDER_MINUTES`.
  - L980-1028 `production_efficiency`: per completed or closed order, planned versus actual minutes and yields.
  - L1032-1091 `machine_utilisation`: busy minutes from *finished* work orders, plus downtime from bookings that name a machine.
- `service.py`:
  - L290-300 `available_minutes(working_hours_per_day, days)` = hours × 60 × **calendar days**. Its ASSUMPTION is "recorded in the module notes" but **is not in docs/assumptions.md**.
  - L499-516 `_derive_production_efficiency` and `_derive_machine_utilisation`.
  - L680-699 the two catalogue entries.
- `schemas.py` L317-348 `ProductionEfficiencyRow` and `MachineUtilisationRow`.
- `models.py` L62-95 `ReportKey` (closed enum); L205 `CheckConstraint("report_key IN (...)", name="report_key_is_known")`.
- **There is no daily production report, no shift board, no operator report and no OEE. No report reads the `shift` column.**

### H. Frontend: how shift is entered and displayed today
**`C:\Project\Other\ATCRAFT\apps\web\src\features\production\`**
- `work-order-dialogs.tsx`:
  - `assignSchema` L80-98 (shift `.max(MAX_SHIFT_LENGTH)`).
  - `buildChanges` L126-149 sends only the changed keys.
  - `WorkOrderAssignDialog` L162-386: operators from `useUserOptions` (L187 → `GET /users`); machines scoped by `useWorkCentreMachineOptions(work_center_id)` (L188); **Shift is a free `<Input maxLength=16 placeholder="A">`** (L316-328).
  - `WorkOrderTransitionDialog` L413-546.
- `work-order-model.ts`:
  - Types are derived from the generated contract (`SchemaOf<'WorkOrderRead'|'WorkOrderUpdate'|...>`, L74-98).
  - `MAX_SHIFT_LENGTH = 16` (L102-103) is a hand-copied bound.
  - `transitionsFrom` L194-202; lookups L305-360.
- `work-orders-page.tsx`:
  - Two tabs: Queue, and Load by work centre.
  - `FILTER_KEYS` L92-99 has **no shift filter**.
  - Operator column L307-322; Shift `Badge` column L323-333.
  - No machine column, by design (docstring L23-28).
- `production-dialogs.tsx`, `entrySchema` L1093-1129 and `RecordProductionDialog` L1146-1399:
  - **The shift field allows `.max(32)` (L1096) while the API allows 16.** A 17-32 character value passes the form and comes back as a 422.
  - The form requires produced or rejected > 0, so a **downtime-only booking cannot be entered**, although the API accepts one.
  - Operators load only if `can(P.USER_VIEW)` (L1161); the machine list is the whole register (L1160).
  - **`work_order_id` is never sent** (payload L1179-1191). Every UI booking is therefore a whole-job, final-output booking: it posts a receipt and never advances an operation.
  - Shift is a free `<Input placeholder="A, B, General...">` (L1242-1253).
  - The Alert at L1374-1379 is wrong (the handbook, `docs/handbook/content.json` L2374, flags it).
- `production-detail-page.tsx` `EntriesPanel` L480-599: shift column L507-512; no operator or machine columns. The empty-state text at L595 repeats the wrong "moves into process" claim.
- `production-model.ts`: **hand-written** `WorkOrderRow` (L244-271) and `ProductionEntryRow` (L274-291), both with `shift`; `MachineOption` L460-464; `OperatorOption` L467-471; `useMachineOptions` L577-597. **Neither mirror is registered** in `tests/unit/test_frontend_contract_types.py`.
- `work-orders-page.test.tsx`: fixture L47-77; L325-348 types 'B' into "Shift" and asserts the PATCH body is `{ shift: 'B' }`; L350-364 checks that no empty PATCH is sent.
- **`C:\Project\Other\ATCRAFT\apps\web\src\hooks\use-user-options.ts`**: the shared staff directory (`GET /users`).
- Routing UI: `features/routing/routing-page.tsx` has tabs Routings / Work centres / Machines, each gated by its own view right (L615-760). `routing-dialogs.tsx` provides `WorkCenterFormDialog` (L1115), `MachineFormDialog` (L1337) and `MachineStatusDialog` (L1580).
- Route wiring: `app/route-loaders.ts` L36 and L54-56; `app/router.tsx` L273, L308-313, L355-359; `app/navigation.ts` Manufacturing group around L193-223.

### I. Docs
- **`C:\Project\Other\ATCRAFT\docs\assumptions.md`** has 38 sections plus open questions. **None covers shifts, operators, machine allocation, capacity, calendars or utilisation.** These ASSUMPTIONs live only in code:
  - `plan_work_orders` (production/service.py L613-618)
  - `total_routing_minutes` (routing/service.py L449-453)
  - `resolve_working_hours_per_day` (L466-469)
  - `available_minutes` (reports/service.py L293-296)
- **`C:\Project\Other\ATCRAFT\docs\database.md`**: §2 module map (routing L63, production L67); "Item attributes" L100-128; §7 state machines L410-438 (no work-order line); §8 integration L440-452. Nothing about shifts.
- **`C:\Project\Other\ATCRAFT\docs\handbook\content.json`** L2305-2307, L2335-2337, L2374 and L2377-2440 describe shift as typed text.

### J. Precedent: the free-text item finish/size/colour became a master (commit `6bc1833`)
**1. Expand — `apps/api/migrations/versions/20260906_0900_item_attributes_master.py`** (rev `4c1d70b8ea92`)
- Creates `item_attributes` and adds six nullable `*_id` columns, each indexed.
- The FKs are added `NOT VALID`, then `VALIDATE CONSTRAINT` (the docstring explains the locking benefit).
- Set-based backfill:
  - The vocabulary is built from both tables, keyed on `upper(btrim(regexp_replace(col,'\s+',' ','g')))`.
  - Each collapse is logged with `log.warning`.
  - The winning spelling is picked with `row_number() OVER (... ORDER BY count(*) DESC, spelling ASC)`. The docstring says "oldest row / UUIDv7", but the SQL tie-breaks on spelling.
  - Code is `left(replace(key,' ','-'),32)`, inserted with `ON CONFLICT DO NOTHING`.
  - An UPDATE then points rows at the ids.
  - Any row whose text did not resolve raises `RuntimeError` instead of losing its value.
- Downgrade is lossless because the text columns are untouched.

**2. Contract — `20260906_0901_drop_item_attribute_text.py`** (`9b6f2ac41d38`)
- A pre-flight query names variant pairs whose distinctness was only a spelling, and refuses.
- Swaps `uq_item_variants_attribute_combo` onto the ids (COALESCE to the nil UUID).
- Drops the text columns.
- Downgrade repopulates the text from the master, so it is a partial rollback: canonical spellings come back, not the originals.

**3. Grant — `20260906_0902_grant_item_attribute_permissions.py`** (`2f5e08c7ba61`, **current head**)
- Idempotent `array_append` of literal permission strings, only to `is_system` roles (SUPER_ADMIN, ADMIN).
- Non-system presets are deliberately untouched.

**API changes**
- `ItemAttribute(TenantMasterEntity)` (`products/models.py` L274-327): kind CHECK, `uq (company_id, attribute, code)`, partial picker index, `sort_order`, `is_active`.
- `ItemAttributeCreate/Update/Out` (`products/schemas.py` L128-161): the code is normalised; Update has no kind field; there is no delete.
- Item inputs and outputs became `finish_id` etc. Outputs carry ids only; the client resolves labels.
- `_require_attribute` (`products/service.py` L1073-1116): a tenant-scoped read, a kind check, and active-on-the-way-in.
- Retiring is refused while the value is in use (L1532-1577).
- Routes `GET/POST /products/attributes` and `PATCH /products/attributes/{id}` (router L185-229).

**Tests**
- `tests/integration/test_products_service.py` gained about 283 lines: normalised code, duplicate code, rename is free, unknown id refused, retired value refused, in-use retire refused, clearing a variant attribute, and **a test that actually lists** — the list endpoint had been answering 500.
- New `tests/unit/test_role_presets_reach_roles.py`.
- Frontend: new `masters/attribute-dialogs.test.tsx`; updated `items-page.test.tsx` (an unset select posts `null`) and `item-detail-page.test.tsx`.

**Also changed in that commit:** `core/demo_data/masters.py`; `core/seed.py` (`_seed_roles` now refreshes system roles); `hooks/use-item-attribute-options.ts`; `lib/permissions.ts`; the regenerated `openapi.json`, `types/openapi.ts` and `postman_collection.json`; `docs/database.md`; and a new CLAUDE.md trap ("A foreign key to a UUID is not a tenant check, and not a kind check").

---

## Missing vs proposal (gap 11 and the gaps that come with it)

1. **No shift master.** There is nowhere to store code, name, start and end time, crosses-midnight, break minutes, active flag or ordering.
2. **No roster or plan.** There is no date × shift × work centre/machine × operator record, no copy-forward or bulk planning, and no publish step.
3. **No conflict rules.**
   - An operator can be double-booked across overlapping shifts.
   - A machine can be staffed twice at the same time.
   - Machine status and `is_active`, and operator `is_active` and internal (non-vendor) status, are never checked on assignment or booking.
4. **No operator directory production roles can read.** Supervisors and plant managers lack `user.view`, so every operator picker is empty for them.
5. **Shift text is ungoverned.**
   - No normalisation, so "A" and "a" are different shifts.
   - The booking form allows 32 characters where the API allows 16.
   - No shift filter on work orders and no index on `work_orders.shift`.
6. **Scheduling ignores shifts.** Operations are planned from 00:00 UTC on `planned_start_date`, back to back, around the clock. `working_hours_per_day` only divides lead time.
7. **No shift-wise reporting.**
   - No daily production or shift report, and no company-wide `GET /production/entries`.
   - No OEE.
   - Machine-utilisation capacity is calendar days × hours. The report itself says "holds no shift calendar".
   - No roster-versus-actual comparison or attendance.
8. **UI bookings cannot record per-operation, per-shift output** because `work_order_id` is never sent, and downtime-only bookings are impossible from the UI.
9. **Calendars are not modelled.** Holidays, weekly offs and minimum rest between shifts are absent. These are open questions, not something to assume.

---

## Patterns to copy

| New piece | Closest precedent (files) | Conventions to follow |
|---|---|---|
| Shift master model | `WorkCenter` / `Machine` (`routing/models.py` L97-236); `ItemAttribute` (`products/models.py` L274-327) | `TenantMasterEntity`; `UniqueConstraint("company_id","code", name="uq_<table>_company_id_code")`; short CHECK names (the convention renders `ck_<table>_<name>`); `is_active` + `sort_order`; partial picker index `WHERE is_active AND deleted_at IS NULL`; Python-side defaults plus `server_default` |
| Master service | `WorkCenterService` / `MachineService` (`routing/service.py` L576-970) | class taking `(session, tenant)`; `async with transaction(self.session)`; `_assert_code_free` → `DuplicateError`; `snapshot(entity, _AUDITED_FIELDS)` before and after; `record_audit(action=CREATE/UPDATE)`; activation through `MasterLifecycleState` + `StateMachine.assert_transition` + `record_status_change(reason=...)`; retire through `referenced_in_use(...)` (`ErrorCode.REFERENCED_RECORD_IN_USE`) and `repo.soft_delete`; `NO_FIELDS_TO_UPDATE` from `src/core/messages.py` |
| Master schemas | `WorkCenterCreate/Update/Read` (`routing/schemas.py` L74-133); `ItemAttributeUpdate` (no immutable fields) | `ConfigDict(extra="forbid")`; `field_validator("code")(_normalise)` (`normalise_code`); `Annotated[..., Field(...)]` bounds; `from_attributes=True` on Read |
| Master router | `/routing/work-centers*` (`routing/router.py` L149-266) | `require_permission(Permission.X)` on every route; `ApiResponse[...].ok(obj, "message")`; `PaginatedResponse`; a `ReasonRequest` body for activate/deactivate; a `ReasonQuery` for DELETE; literal routes before `/{id}` |
| Tenant + kind + active check on a FK | `_require_attribute` (`products/service.py` L1073-1116); `_assert_machine` (production L968-990) | read through the tenant-scoped repository; `NotFoundError` for another tenant's row; refuse retired values on the way in, keep them on the way out |
| Roster rows with status | `WorkOrder` + `WORK_ORDER_FLOW` + `_transition` (production/service.py L284-315, L840-877) | a `StatusEnum` column (`inventory/models.py` L318); a `StateMachine` declared in `service.py` with a human `entity` name; `record_status_change` in the same transaction |
| Composite machine FK | `fk_work_orders_machine_id_work_center_id_machines` (production/models.py L551-556) | `ForeignKeyConstraint(["machine_id","work_center_id"], ["machines.id","machines.work_center_id"], ondelete="RESTRICT")` |
| Overlap check | `FinancialYearRepository.overlapping_active` + `_assert_no_active_overlap` (`organization/repository.py` L492-525, `organization/service.py` ~L1176-1199) | inclusive/half-open predicate; `exclude_id` so an edit does not collide with itself; the docstrings name the `btree_gist` EXCLUDE constraint as the absolute guard |
| Race guard | `advisory_xact_lock(session, "key", company_id, ...)` (`src/core/database.py` L206-221; used in bom/routing/sales/mrp) | take keys in sorted order |
| Batched validation | `_assert_line_positions` (production L898-956); `MachineRepository.load` / `WorkCenterRepository.rates` | two statements per document, never one per line |
| Bounded batch and report-all | `MAX_ISSUE_LINES`, `MAX_OPERATIONS_PER_ROUTING`; `release_order` shortages list | a hard cap on bulk size; report every conflict in `details`, not just the first |
| Projection list with joined labels | `ProductionOrderRepository.wip_page` / `WorkOrderRepository.load_by_work_centre` | `core.pagination.paginate` reads `.scalars()`, so a projection must reproduce `has_next` with limit+1 itself (repository ~L582) |
| Read-only cross-module seam | `in_progress_output` (production/service.py L3407-3441) | a signature-is-contract function another module calls |
| Report | `ReportKey` + `ReportDefinition` in `REPORT_CATALOGUE` (reports/service.py L680-699) + route (`router.py` L529-580) + `_derive_*` | the catalogue asserts completeness at import; widen `report_key IN (...)` through a migration (`20260905_0100_report_snapshots_packing_list_key.py`) |
| Expand migration | `20260906_0900_item_attributes_master.py` | review block (locking / data / rollback / size / indexes); FK `NOT VALID` then `VALIDATE`; literal values, never imported enums |
| Permission grant migration | `20260906_0902_grant_item_attribute_permissions.py` | literal strings; `is_system` roles only; idempotent |
| Migration test | `tests/integration/test_mrp_late_supply_migration.py` | subprocess `alembic` with `timeout=` and `stdin=DEVNULL`; explicit `REVISION`/`PARENT`; round trip |
| Integration test layout | `tests/integration/test_production_service.py` L96-165; `test_routing_service.py` | one schema per module; ORM seeding (`tests/seed_support.py`); tenant probe tests |
| Concurrency test | `tests/integration/test_allocation_concurrency.py`, `test_bom_concurrency.py` | two sessions racing |
| Frontend master tab and dialog | `features/masters/attribute-dialogs.tsx` + test; `features/routing/routing-dialogs.tsx` `WorkCenterFormDialog` | zod schema, `zodResolver`, `applyFieldErrors`, `success.created(...)` from `lib/messages.ts` |
| Picker hook | `hooks/use-item-attribute-options.ts` | `LOOKUP_LIST_LIMIT`; `emptyOnPermissionError`; `staleTimes.reference`; a labeller that falls back for retired values |
| Board page with tabs and URL filters | `features/production/work-orders-page.tsx` | `FILTER_KEYS`; one `setSearchParams` write; `useListQuery` + `DataTable`; dialogs mounted only with the manage right |
| Frontend test | `work-orders-page.test.tsx` | `renderScreen(..., { permissions, routes })`; gate tested with and without the right; `fetch.requestFor` asserts the exact body; keep Popper menu openings to a minimum |

---

## Integration points

**Production**
- `work_orders` and `production_entries`: add `shift_id`, and keep `shift` as a code snapshot.
- `update_work_order` (L3136), `_book_entry` (L2869), `complete_order` (L1765) and `create_production_entry` (L3042) must validate `shift_id` and write the snapshot.
- `WorkOrderFilters` and `ProductionEntryFilters` gain `shift_id`.
- Optional: a company-wide `GET /production/entries`, declared before `/{order_id}`.
- Costing is unaffected, because it uses actual timestamps.

**Routing**
- Roster validation reads `WorkCenterRepository.rates` (`is_active`) and `MachineRepository.load` (`status`, `is_active`, `work_center_id`).
- `WorkCenterService.retire_work_center`, `MachineService.retire_machine`, `MachineService.update_machine` (moving a machine to another centre) and `change_status → RETIRED` need **"used by a live roster"** probes. They already have "used by live routing" probes; otherwise a planned future row points at a retired or moved asset. Moving a machine named on assignments would otherwise fail the composite FK with a generic 409, which is what already happens today for work orders.

**Users**
- Operator eligibility needs `is_active`, `vendor_id IS NULL` and `deleted_at IS NULL`.
- A new production-readable directory is needed.
- Optionally, user deactivation should consider future roster rows. That is an open question.

**Organization**
- `companies.timezone` (default `Asia/Kolkata`, `organization/models.py` L59) is the zone in which shift wall-clock times are read.
- The `_TZDB_AVAILABLE` guard is in `organization/schemas.py` L36-45.
- `tzdata` is only a transitive dependency (`requirements.lock`: "via kombu").

**Reports**
- New shift-production report.
- Roster-based capacity columns on machine utilisation.
- `report_snapshots` CHECK constraint.
- Frontend `features/reports/reports-types.ts` `ReportKey` union.

**Permissions and seed**
- `Permission` enum, `PERMISSION_GROUPS` (L210-241), `ROLE_PRESETS`.
- `_seed_roles` (`core/seed.py` L193) refreshes system roles only.
- A grant migration is needed for deployed systems.

**Audit:** `record_audit` / `record_status_change` with new `entity_type` values, in the same transaction.

**Notifications (optional):** a `NotificationType` member such as `SHIFT_ASSIGNED`. That column has no DB CHECK, so no migration is needed. Push and SMS stay out of scope.

**MRP (later, deliberately not now):** `manufacture_lead_time_days` could derive hours per day from shifts, but that would move every existing plan.

**Demo data:**
- `core/demo_data/engineering.py`: seed shifts.
- `operations.py` (L607, L634-635, L944, L1088): bookings pass `shift="A"/"B"`.
- `masters.py` `STAFF`: there are no operator users today.

---

## Traps and constraints that a change here can trip

1. **Do not use generated or server-side window columns.** If `starts_at`/`ends_at` were GENERATED or server-computed, SQLAlchemy would expire them after an UPDATE and serialising would raise `MissingGreenlet`. Compute them in Python and store them as plain columns (CLAUDE.md, the `onupdate` trap).
2. **Exclusion and partial-unique constraints are checked per statement.** When swapping two operators or replacing a day's roster, cancel or free the old rows before inserting the new ones. Keep the constraint IMMEDIATE: a DEFERRED violation would surface at the outer COMMIT in `get_db` with a generic error.
3. **Declare the EXCLUDE constraint on the model.** The initial migration's docstring (`20260831_2130_initial_schema.py` L106-112) warns that a constraint present only in a migration drifts from the metadata.
   - Use `sqlalchemy.dialects.postgresql.ExcludeConstraint`. SQLAlchemy 2.0.52 accepts `func.tstzrange(...)` elements.
   - Give it an explicit name. The naming convention has no `ex` key, and names must stay within 63 bytes (`tests/unit/test_schema_identifier_length.py`).
   - Run `alembic revision --autogenerate` after applying the migration and confirm the diff is empty.
4. **`btree_gist` reaches every integration suite.** Every module-scoped integration file runs `Base.metadata.create_all` against `settings.database_url` and runs only `CREATE EXTENSION IF NOT EXISTS pg_trgm` — 23 files, for example `test_production_service.py` L119-120. A GiST `uuid WITH =` exclusion needs `btree_gist`. It is present through `infrastructure/docker/postgres/init/01-extensions.sql`, the initial migration and `7f3a91c4e5d2`, and `tests/conftest.py` L147-148 creates it for the shared DB. If it is missing on some database, all 23 files fail.
5. **A foreign key to a UUID is not a tenant check.** `update_work_order` has a blind `setattr` loop (L3179-3180). `shift_id` must be read through a tenant-scoped repository and checked for active state *before* that loop. The same applies in `_book_entry` (CLAUDE.md, the `finish_id` trap).
6. **`model_validate(orm_row)` reads every declared field.** Do not add relationships to `WorkOrderRead` or `ProductionEntryRead`; use ids, snapshots or projections. `get_order_detail` (L1048-1065) already builds the detail field by field for this reason.
7. **Ordering ties.** Order roster lists by `(roster_date, starts_at, id)` and audit by `(created_at, id)`. `created_at` is transaction-start time and ties within one transaction.
8. **List mechanics.**
   - `list_page(order_by=...)` takes **one** clause. A tuple caused the item-attribute 500.
   - `paginate` reads `.scalars()`.
   - Use `x if x is None else y`, never `or`, when the left side can be a SQL clause.
   - An empty multi-select filter must yield an empty result. A bare `if xs:` with no else inverts the filter.
   - Every sortable column must be listed in `sortable_fields`.
9. **Status badge colours.** `tests/unit/test_status_badge_catalogue.py` requires every `*Status`/`*State` enum value to appear in `STATUS_VARIANTS` (`apps/web/src/components/shared.tsx` L112). **`PLANNED` and `PUBLISHED` are not there today.**
10. **Permissions must be wired everywhere.**
    - Every new resource must appear in `PERMISSION_GROUPS` (`tests/unit/test_role_presets_reach_roles.py`).
    - Every backend permission must be in `apps/web/src/lib/permissions.ts` (`test_frontend_permission_catalogue.py`).
    - Every mutation must be gated in the calling feature folder on the right its endpoint enforces (`test_frontend_permission_gates.py`).
    - Deployed non-system roles (PRODUCTION_SUPERVISOR, PLANT_MANAGER) do **not** receive new permissions automatically. Either gate picker reads with `require_any_permission(...)` (`src/core/deps.py` L309) on rights those roles already hold, or document that an admin must grant them.
11. **Hand-written mirrors.** `WorkOrderRow` and `ProductionEntryRow` (`production-model.ts`) are not compared with anything. Register them in `MIRRORS` (`tests/unit/test_frontend_contract_types.py`) or derive them from `SchemaOf`.
12. **Routing and mounting.**
    - Literal routes must come before `/{order_id}` (production) and `/{routing_id}` (routing).
    - A new module is **never mounted** unless it is added to `MODULE_ORDER` (`src/api/router.py` L23-49).
    - `docs/architecture.md` L152-166 says dependencies point **downward only**.
13. **Contract and docs tests.**
    - `make contract` regenerates `openapi.json`, `apps/web/src/types/openapi.ts` and `postman_collection.json`.
    - `tests/unit/test_postman_and_contract_targets.py` requires `docs/api.md` to state the exact "**494 requests in 27 folders**" count (L28 today). Any new endpoint or tag breaks it until the doc is updated.
14. **Sweep scripts.** `tests/unit/test_e2e_sweep_routes.py`: every new static screen must be listed in `tests/e2e/scripts/browser-sweep.mjs`, `a11y-sweep.mjs` and `responsive-sweep.mjs`, and `permission-sweep.mjs` must name one of the route's own permissions. Also update `data-sweep.mjs`, `tenant-isolation.mjs` and the handbook's `capture-screens.mjs`.
15. **New report keys.** Each one needs a migration that swaps `ck_report_snapshots_report_key_is_known` (the precedent lists keys literally), a catalogue definition, a route, and the frontend `ReportKey` union.
16. **Never call `session.rollback()` in a service.** Let an exclusion `IntegrityError` propagate. SQLSTATE 23P01 currently falls through to `_GENERIC_CONFLICT` (409, "The operation conflicts with existing data.") in `src/core/exception_handlers.py` L131-207. Optionally add an explicit 23P01 mapping with a unit test. Keep the service pre-check so users see a named conflict.
17. **Business date versus UTC.**
    - `today()` is a UTC date, and `_book_entry` defaults `entry_date` to it.
    - Receipts are stamped at midnight UTC on `entry_date`.
    - Define `roster_date` as the **local date on which the shift starts**. Never derive it from UTC instants, or a night shift lands on the wrong day.
18. **Retired machines that are not deleted.** `MachineService.change_status(RETIRED)` does not soft-delete, so `MachineRepository.load` still returns retired machines. Check `status` and `is_active` explicitly.
19. **Test data.**
    - No raw `INSERT` in tests; build rows through `tests/seed_support.py`.
    - `test_routing_service.py` has its own local `seed_work_center` and `seed_machine`. Move them up into `seed_support` instead of copying them a third time.
    - Never hand-write another module's table in a test.
20. **Frontend constants and helpers.**
    - Page and limit numbers must come from `lib/constants.ts` (`test_frontend_constants.py`).
    - There must be exactly one `applyFieldErrors` (`test_frontend_field_errors.py`).
    - The jsdom `:modal` shim lives in `src/test/setup.ts` and must not be copied into test files. Each Radix Popper mount costs about 20 s under jsdom (`work-orders-page.test.tsx` docstring).
21. **Demo idempotence.** If the demo loader seeds roster rows, key the existence check on identity (operator, shift, date offset), not on a clock-derived value.
22. **Migration heads.** The current head is `2f5e08c7ba61`. Parallel work (sales invoice, customer return, …) will add revisions, so set `down_revision` at merge time and check `alembic heads`.
23. **Record new ASSUMPTIONs.** Any new `ASSUMPTION:` must also be added to `docs/assumptions.md`. That file has no production or capacity entries at all today.
24. **Stale text worth fixing in passing.**
    - `WorkCentreRateRepository`'s docstring (`production/repository.py` L328-335) says the rate is "not snapshotted", but `WorkOrder` does snapshot it.
    - `test_a_machine_under_maintenance_cannot_be_handed_a_job` (`test_routing_service.py` L723) tests only the machine state machine, not work-order assignment.

---

## Files a change here will touch

**Backend: new**
- `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\<date>_shift_master_and_roster.py`
- `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\<date>_grant_shift_permissions.py`
- `C:\Project\Other\ATCRAFT\apps\api\migrations\versions\<date>_report_snapshots_shift_production_key.py` (only if the report is built)

**Backend: modified**
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\routing\models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py` (shift master, roster, retire/move probes)
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\production\models.py`, `schemas.py`, `service.py`, `repository.py`, `router.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\reports\models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\core\permissions.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\core\exception_handlers.py` (optional 23P01 mapping)
- `C:\Project\Other\ATCRAFT\apps\api\src\core\demo_data\engineering.py`, `operations.py`, `masters.py`
- `C:\Project\Other\ATCRAFT\apps\api\src\modules\notifications\models.py` (optional)
- `C:\Project\Other\ATCRAFT\apps\api\src\api\router.py` (only if a new module is chosen instead of routing)

**Backend tests**
- New:
  - `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_shift_windows.py`
  - `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_shift_schemas.py`
  - `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_shift_roster_service.py`
  - `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_shift_roster_concurrency.py`
  - `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_shift_master_migration.py`
- Modified:
  - `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_production_service.py` (L756 `shift="A"`, plus the first `update_work_order` and booking-with-shift tests; none exist today)
  - `C:\Project\Other\ATCRAFT\apps\api\tests\integration\test_routing_service.py` (probes)
  - `C:\Project\Other\ATCRAFT\apps\api\tests\seed_support.py` (`seed_work_center`, `seed_machine`, `seed_shift`)
  - `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_frontend_contract_types.py` (MIRRORS)
  - `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_reports_catalogue.py` (if a report is added)
  - `C:\Project\Other\ATCRAFT\apps\api\tests\unit\test_exception_handlers.py` (if 23P01 is mapped)

**Contract and docs**
- `C:\Project\Other\ATCRAFT\openapi.json`
- `C:\Project\Other\ATCRAFT\postman_collection.json`
- `C:\Project\Other\ATCRAFT\apps\web\src\types\openapi.ts`
- `C:\Project\Other\ATCRAFT\docs\api.md` (request/folder count and endpoint map)
- `C:\Project\Other\ATCRAFT\docs\database.md` (§2, new "Shifts and the roster" section, §7)
- `C:\Project\Other\ATCRAFT\docs\assumptions.md` (new sections)
- `C:\Project\Other\ATCRAFT\docs\architecture.md` (only if a module is added)
- `C:\Project\Other\ATCRAFT\docs\handbook\content.json` and `screens\`
- `C:\Project\Other\ATCRAFT\CLAUDE.md` (a trap note if the EXCLUDE work reveals one)

**Frontend: new**
- `C:\Project\Other\ATCRAFT\apps\web\src\features\shifts\shift-model.ts`
- `C:\Project\Other\ATCRAFT\apps\web\src\features\shifts\shift-dialogs.tsx` (+ `.test.tsx`)
- `C:\Project\Other\ATCRAFT\apps\web\src\features\shifts\shift-roster-page.tsx` (+ `.test.tsx`)
- `C:\Project\Other\ATCRAFT\apps\web\src\features\shifts\roster-dialogs.tsx`
- `C:\Project\Other\ATCRAFT\apps\web\src\hooks\use-shift-options.ts`
- `C:\Project\Other\ATCRAFT\apps\web\src\hooks\use-operator-options.ts`

**Frontend: modified**
- `C:\Project\Other\ATCRAFT\apps\web\src\app\route-loaders.ts`, `router.tsx`, `navigation.ts`
- `C:\Project\Other\ATCRAFT\apps\web\src\lib\permissions.ts`, `query-client.ts`
- `C:\Project\Other\ATCRAFT\apps\web\src\components\shared.tsx` (`PLANNED`)
- `C:\Project\Other\ATCRAFT\apps\web\src\features\production\work-order-dialogs.tsx`, `work-order-model.ts`, `work-orders-page.tsx`, `work-orders-page.test.tsx`, `production-dialogs.tsx`, `production-model.ts`, `production-detail-page.tsx`
- `C:\Project\Other\ATCRAFT\apps\web\src\features\reports\reports-types.ts`

**E2E scripts**
- `C:\Project\Other\ATCRAFT\tests\e2e\scripts\browser-sweep.mjs`, `a11y-sweep.mjs`, `responsive-sweep.mjs`, `permission-sweep.mjs`, `data-sweep.mjs`, `tenant-isolation.mjs`, `capture-screens.mjs`

---

## Recommended design: Shift master, Shift roster, and how production references a shift

### D1. Where it lives
**Put `shifts` and `shift_assignments` in the `routing` module.**
- Routing already owns work centres, machines and the only hours figure.
- Routing's retire and move operations need "in use by roster" probes. With the roster inside routing, those probes are not an upward or sideways import (`docs/architecture.md`: dependencies point downward only).
- Production (execution) and reports (downstream) already import routing.
- `MODULE_ORDER` is unchanged.
- Routes go under `/routing/shifts…` and `/routing/roster…`, declared **before** `/{routing_id}`. They use the existing tag, so the Postman folder count is unchanged; the request count still changes.

The alternative is a new `shifts` module. It needs a `MODULE_ORDER` entry, and routing would then import it sideways for the probes.

### D2. Shift master: `shifts` (`TenantMasterEntity`)
```python
class Shift(TenantMasterEntity):
    __tablename__ = "shifts"
    code: Mapped[str] = mapped_column(String(16), nullable=False)   # = width of the existing work_orders/production_entries.shift snapshot
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)  # wall clock in companies.timezone (first TIME column in the schema)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    crosses_midnight: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    break_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_shifts_company_id_code"),
        CheckConstraint("(crosses_midnight AND end_time <= start_time) OR (NOT crosses_midnight AND end_time > start_time)",
                        name="midnight_flag_matches_times"),
        CheckConstraint("break_minutes >= 0 AND break_minutes < 1440", name="break_minutes_bounded"),
        Index("ix_shifts_company_picker", "company_id", "sort_order",
              postgresql_where=text("is_active AND deleted_at IS NULL")),
    )
```

**Rules**
- `code` goes through `normalise_code` and is **immutable**: `ShiftUpdate` omits it, as `ItemAttributeUpdate` omits the kind. The code is what gets snapshotted into production rows.
- `break_minutes` must be less than the duration. Validate that in the schema validator.
- A start time equal to the end time with `crosses_midnight` means a 24-hour shift.
- Changing the times, or deactivating or retiring the shift, is refused with `referenced_in_use` while PLANNED assignments exist on or after today. To change a shift that is in use, retire it and create a new one. Record this as an ASSUMPTION.
- Activation and deactivation go through `MasterLifecycleState` and a `SHIFT_LIFECYCLE` state machine, as with work centres.
- **Pure helpers** in `routing/service.py`, unit-tested:
  - `shift_minutes(start, end, crosses_midnight) -> int`
  - `net_shift_minutes(shift) -> int` (duration minus break)
  - `shift_window(roster_date, shift, tz) -> (starts_at_utc, ends_at_utc)`: combine the local date and time in `ZoneInfo(companies.timezone)`, add one day to the end when the shift crosses midnight, convert to UTC.
  - `windows_overlap(a, b)`: half-open, so A 06-14 and B 14-22 do not clash.
- **Endpoints:**
  - `GET /routing/shifts` (paginated; `search`, `is_active`; sort `sort_order`/`code`) and `GET /routing/shifts/{id}`, gated `require_any_permission(SHIFT_VIEW, SHIFT_ROSTER_VIEW, WORK_ORDER_VIEW, PRODUCTION_ENTRY_CREATE)` so existing production roles can fill pickers without role edits.
  - `POST /routing/shifts` (201), `PATCH /routing/shifts/{id}`, `POST /routing/shifts/{id}/activate|deactivate` (reason body), `DELETE /routing/shifts/{id}?reason=` — all `SHIFT_MANAGE`.
- Audit `entity_type="shift"`.

### D3. Roster: `shift_assignments` (`TenantEntity`, status-driven, no hard delete)
```python
class ShiftAssignmentStatus(StrEnum):
    PLANNED = "PLANNED"
    CANCELLED = "CANCELLED"

class ShiftAssignment(TenantEntity):
    __tablename__ = "shift_assignments"
    roster_date: Mapped[date]                       # LOCAL date the shift starts (night shift 22-06 on D is D)
    shift_id        -> FK shifts.id RESTRICT, NOT NULL
    work_center_id  -> FK work_centers.id RESTRICT, NOT NULL
    machine_id      nullable; composite FK (machine_id, work_center_id) -> machines(id, work_center_id) RESTRICT
                    (NULL = staffed at cell level, e.g. one operator tending several machines)
    operator_user_id -> FK users.id RESTRICT, NOT NULL
    starts_at, ends_at: TIMESTAMPTZ NOT NULL         # snapshot computed in Python at write time; a later timezone change does not move them (ASSUMPTION)
    status: StatusEnum(ShiftAssignmentStatus) default PLANNED
    notes: Text | None
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="window_ordered"),
        ExcludeConstraint((column("company_id"), "="), (column("operator_user_id"), "="),
                          (func.tstzrange(column("starts_at"), column("ends_at"), literal_column("'[)'")), "&&"),
                          using="gist", where=text("status = 'PLANNED'"),
                          name="ex_shift_assignments_operator_window"),
        ExcludeConstraint((column("company_id"), "="), (column("machine_id"), "="), (<same range>, "&&"),
                          using="gist", where=text("status = 'PLANNED' AND machine_id IS NOT NULL"),
                          name="ex_shift_assignments_machine_window"),   # ASSUMPTION: one operator per machine at a time
        Index("ix_shift_assignments_grid", "company_id", "roster_date", "work_center_id", "shift_id"),
        Index("ix_shift_assignments_operator", "company_id", "operator_user_id", "starts_at"),
    )
```

**State machine.** `SHIFT_ASSIGNMENT_FLOW = StateMachine(entity="Shift assignment", initial=PLANNED, transitions=(Transition(PLANNED, CANCELLED, permission=Permission.SHIFT_ROSTER_MANAGE, label="Cancel", is_irreversible=True),), terminal=frozenset({CANCELLED}))`. Declare it in `routing/service.py` so `test_state_machine_entity_names.py` scans it.

**Validation**, batched for single and bulk writes alike, inside `transaction()`:
1. Take `advisory_xact_lock(session, "shift_roster", company_id)` so the friendly pre-check cannot race.
2. Load in batches:
   - shifts: tenant, active, not deleted;
   - work centres: `WorkCenterRepository.rates`, must be `is_active`;
   - machines: `MachineRepository.load`, must be in the stated centre, `is_active`, status not RETIRED. MAINTENANCE and BREAKDOWN produce a warning, not a block, matching the `WORKABLE_MACHINE_STATUSES` precedent;
   - operators: a new projection, `is_active AND vendor_id IS NULL AND deleted_at IS NULL`.
3. Compute each window with `shift_window`.
4. Run **one** query for existing PLANNED rows of the involved operators and machines whose windows touch `[min_start, max_end)`. Then check overlaps in Python, including overlaps inside the batch, excluding the row being edited (`exclude_id`).
5. Raise `BusinessRuleError` listing **every** conflict in `details` (operator or machine, date, shift code, work centre). The EXCLUDE constraint is the backstop for a race.
6. Insert, flush, and write `record_audit(CREATE, entity_type="shift_assignment", entity_code=f"{roster_date} {shift.code}")`. Cancelling uses `record_status_change` with a reason.

**Endpoints**, all under `/routing/roster`, declared before `/{routing_id}`:
- `GET /routing/roster/assignments` (`SHIFT_ROSTER_VIEW`):
  - Filters: `date_from` and `date_to` (required, at most 31 days, named constant), `shift_id`, `work_center_id`, `machine_id`, `operator_user_id`, `status`.
  - Returns a projection row with `shift_code`, `work_center_code`, `machine_code`, `operator_name`, `employee_code` joined in tenant scope. Pagination is reproduced as in `wip_page`.
- `POST /routing/roster/assignments` (201), `POST /routing/roster/assignments/bulk` (at most `MAX_ROSTER_BULK`, all or nothing), `POST /routing/roster/assignments/copy` (`{source_date, target_from, target_to, work_center_id?}`, bounded), `PATCH /routing/roster/assignments/{id}` (operator, machine, work centre, notes; re-validated), `POST /routing/roster/assignments/{id}/cancel` (reason). All `SHIFT_ROSTER_MANAGE`.
- `GET /routing/roster/operators`, gated `require_any_permission(SHIFT_ROSTER_VIEW, WORK_ORDER_MANAGE, PRODUCTION_ENTRY_CREATE)`. Paginated `{id, full_name, employee_code, plant_id, department_id}` of active internal users. This also fixes the empty operator pickers for supervisors.

**Permissions**
- New: `shift.view`, `shift.manage` (add "shift" to the "Engineering" group) and `shift_roster.view`, `shift_roster.manage` (add "shift_roster" to "Production").
- Presets: `_PRODUCTION` += `SHIFT_VIEW`, `SHIFT_ROSTER_VIEW`, `SHIFT_ROSTER_MANAGE`; PLANT_MANAGER += `SHIFT_MANAGE`; PLANNER += the two view rights.
- A grant migration for system roles. Document that deployed non-system roles must be granted these by an administrator.

**Retire/move probes.** Add `used_by_live_roster(...)` to `WorkCenterRepository` and `MachineRepository`. Call it from `retire_work_center`, `retire_machine`, `update_machine` (when the work centre changes) and `change_status(RETIRED)`. "Live" means a PLANNED row with `roster_date >= today()`.

### D4. How work orders and production entries reference a shift
**Recommended: add a nullable FK and keep the existing text column as a read-only code snapshot.**

**Schema**
- Add `shift_id` (FK `shifts.id` ON DELETE RESTRICT) to `work_orders` and `production_entries`.
- Indexes: `ix_work_orders_shift (company_id, shift_id, status)` and `ix_production_entries_shift (company_id, shift_id, entry_date)`.
- **Keep the `shift` String(16) column.** The service writes `shift = shift.code` whenever `shift_id` is set and clears both together.
- Replace the comment at `production/models.py` L490-491 with the snapshot rationale. This is the same pattern as `operation_code`, `operation_name` and the rate snapshots on the same row.

**API**
- Outputs gain `shift_id` (`WorkOrderRead`, `ProductionEntryRead`); `shift` stays.
- Inputs `WorkOrderUpdate`, `ProductionEntryCreate` and `CompleteRequest` take `shift_id`, and **drop free-text `shift`**. That is the governance step, as with item attributes. Update every in-repo caller:
  - `core/demo_data/operations.py` L607, L634-635, L944, L1088
  - `tests/integration/test_production_service.py` L756
  - the two frontend dialogs
- Filters gain `shift_id`. The text `?shift=` filter stays, and it still covers new rows because of the snapshot.

**Service**
- New `_assert_shift(session, tenant, shift_id)`: tenant-scoped, active on the way in.
- `update_work_order` handles `shift_id` explicitly **before** the blind `setattr` and audits both fields.
- `_book_entry` and `complete_order` set the snapshot the same way.
- The roster is **advisory** for assignments and bookings: a mismatch adds a warning to the message and never blocks, because a booking is a fact. Record this as an ASSUMPTION with a named constant. The UI pre-fills the shift from the roster for the chosen operator and date.

**No automatic backfill of legacy text, and why**
- A shift master row needs times. Legacy text has none, and inventing them breaks CLAUDE.md's "do not invent a business rule silently".
- Legacy rows keep their text with `shift_id NULL`. Reports group by the snapshot text, so legacy and new rows sit together.
- Optional later step: an admin-triggered, set-based, audited "link legacy text" action. It would set `shift_id` where `upper(btrim(shift)) = code AND shift_id IS NULL`, reusing the normalisation SQL from `4c1d70b8ea92`.
- Not recommended: copying the full item-attribute expand/contract (dropping the text). That would break the `ix_production_entries_date` index, the `?shift=` filter and the demo data, and still could not create valid master rows.

### D5. Reports
- **`SHIFT_PRODUCTION`** — "Daily production by shift":
  - Groups `production_entries` by `entry_date`, `shift` (snapshot), work centre (through `work_orders`), machine and operator.
  - Columns: produced, rejected, downtime, number of bookings; derived yield %.
  - Needs the CHECK-swap migration, a catalogue entry, `GET /reports/shift-production` and the frontend union.
- **Machine utilisation:** add `rostered_minutes` (net PLANNED minutes per machine in the period) and `rostered_utilisation_percent`. **Leave `available_minutes` unchanged** so existing figures do not move.
- OEE comes later. Availability = (rostered net − downtime) / rostered net; performance = ideal run minutes from the routing / busy minutes; quality = good / (good + rejected).

### D6. Migrations (chain off the head at merge time; today that is `2f5e08c7ba61`)
1. **`shift_master_and_roster`.**
   - Create `shifts` and `shift_assignments`, including both EXCLUDE constraints and the composite FK.
   - Add `shift_id` to `work_orders` and `production_entries`: `ADD COLUMN` (catalogue-only), FK `NOT VALID` then `VALIDATE`, plus the indexes.
   - No data migration. The downgrade is lossless (the text columns are untouched).
   - Include the review block: locking, data, rollback, size, indexes.
2. **`grant_shift_permissions`.** Literal strings, `is_system` roles only, idempotent, reversible.
3. **`report_snapshots_shift_production_key`**, if the report is built. Literal key list, `NOT VALID` + `VALIDATE`, and a downgrade that deletes rows with the new key.
- Add `test_shift_master_migration.py`: the parent and child revisions are explicit, it round-trips, and the table columns match the models.

### D7. Frontend
**New page `/production/shift-roster`** ("Shift roster"):
- Router guard `[P.SHIFT_ROSTER_VIEW, P.SHIFT_VIEW]`; listed in the Manufacturing navigation.
- **Tabs:**
  - *Day board* — rows are work centres, then machines; columns are active shifts by `sort_order`; cells are operator chips with Cancel, plus Assign. Data comes from `GET …/assignments` filtered by date and work centre with `LOOKUP_LIST_LIMIT`. Narrow by work centre when `has_next`. Never fetch everything and filter. On a phone it becomes a card list (watch the `shrink-0`/`flex-wrap` and dialog-overflow traps).
  - *Week* — a read-only operator × day matrix.
  - *Shifts* — the master list with `ShiftFormDialog`: code, name, `<input type="time">` start and end, crosses-midnight derived and shown, break minutes, sort order.
- Hooks:
  - `use-shift-options.ts`: copy of the item-attribute hook, with a labeller that falls back to the snapshot text for legacy rows.
  - `use-operator-options.ts`: the new directory, replacing `useUserOptions` in the production pickers.

**Changes to existing production screens**
- `WorkOrderAssignDialog`: Shift becomes a select by `shift_id`, showing the window. Legacy text is shown read-only. Optionally add "rostered for this cell on <local date of planned_start_at>".
- `RecordProductionDialog`: Shift select; remove the 32-character free text; operator directory; pre-fill from the roster. Fix the wrong Alert and empty-state text while there. Optionally add a work-order select.
- Work orders page: shift filter on `shift_id`.

**Wiring:** add `PLANNED` to `STATUS_VARIANTS`; four `P.*` constants; a `queryKeys.shifts` branch.

**Tests**
- New `shift-roster-page.test.tsx`: each gate tested with and without the right; exact POST body; a conflict error is surfaced.
- New `shift-dialogs.test.tsx`.
- Update `work-orders-page.test.tsx` L325-364 to pick a shift option instead of typing 'B'.

### D8. Backend tests
- **Unit** (`test_shift_windows.py`):
  - cross-midnight;
  - 24-hour shift;
  - back-to-back shifts are not an overlap;
  - General 09:00-17:30 overlaps A and B;
  - the night shift on D against A on D+1;
  - break bounds;
  - the local-date versus UTC-date edge in IST.
- **Schemas:** code normalisation, immutable code, time/flag consistency.
- **Integration** (`test_shift_roster_service.py`, one schema per module):
  - another tenant's shift, machine or operator is refused;
  - an inactive or vendor-portal operator is refused;
  - a machine from another centre or a retired machine is refused;
  - operator and machine double-booking is refused with every conflict named;
  - editing a row does not conflict with itself;
  - cancel is audited;
  - retire/move probes;
  - listing actually lists (the item-attribute lesson).
- **Production:** the first tests ever for `update_work_order`: `shift_id` writes the snapshot; another tenant's or a retired shift is refused; the snapshot survives a rename of the shift's name. A booking with `shift_id` is covered too.
- **Concurrency:** two sessions racing for one operator window. Exactly one succeeds; the other gets a 409 or the business-rule error.
- **Meta:** register the production mirrors in `test_frontend_contract_types.py`.

### D9. New ASSUMPTIONs for `docs/assumptions.md` (plus the existing code-only ones listed in Exists today §I)
- The roster date is the local date on which a shift starts. Shift times are interpreted in `companies.timezone`. Assignment windows are snapshots.
- An operator holds at most one PLANNED window at a time. A machine has at most one operator at a time. Cell-level rows (no machine) cover multi-machine tending.
- The roster is advisory for work-order assignment and bookings: it warns and never blocks.
- A shift code is immutable, and a shift in use cannot be re-timed or retired.
- Rostered capacity is added beside calendar capacity in utilisation; it does not replace it.

**Open questions for Atcraft**
- Operators must currently be login users with an email address. Is an employees-without-login master needed?
- Helpers or second operators on one machine?
- Holidays and weekly offs?
- A minimum rest period between shifts?
- A publish/lock step for the weekly plan, which would need a header table and a DRAFT → PUBLISHED status (`PUBLISHED` would then need a badge colour)?
