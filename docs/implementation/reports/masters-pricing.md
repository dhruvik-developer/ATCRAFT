# Area map: masters and pricing (gaps 5 and 6, plus dealer pricing)

Paths are relative to `C:\Project\Other\ATCRAFT\apps\api\` for the backend and `C:\Project\Other\ATCRAFT\apps\web\src\` for the frontend, unless stated otherwise. The current Alembic head is `2f5e08c7ba61` (`migrations/versions/20260906_0902_grant_item_attribute_permissions.py`), so the next migration has `down_revision = "2f5e08c7ba61"`.

**Checking the audit's claims for this area:**

| Audit claim | Verdict | Evidence |
|---|---|---|
| 5. No cost-centre master | **True. Nothing exists anywhere.** | A case-insensitive grep for `cost[ _-]?cent(re\|er)` over the whole repository (code, migrations, docs, frontend, handbook) finds nothing. The proposal (PDF p.6, "Phase 02 Foundation Masters > Organization") lists "Cost center master". The closest thing that exists is `Department`, whose docstring says "Used for cost attribution and approval routing" (`src/modules/organization/models.py:120`). No finance, report or costing code reads departments. |
| 6. No brand or manufacturer master | **True.** There is no table, no column, no enum and no free-text field. | A grep for `brand\|manufacturer` in `apps/api/src` finds only comments. In `apps/web/src` it finds only CSS and the login panel. The proposal (PDF p.3) lists "Category, brand, manufacturer & specification masters" and "Dealer pricing & discount management" as modules of the existing system. |
| Dealer pricing (part of gap 1, the dealer site) | **Partly built.** | Channel price lists (SALE, DEALER, DISTRIBUTOR), quantity slabs, validity windows, variant-specific rows, an item-master `dealer_price` fallback and `customers.price_list_type` all exist. Missing: customer-specific prices, dealer or brand discounts, MRP, a bulk catalogue price endpoint, any dealer principal, and any record of which price-list row priced a sales line. |

---

## Exists today

### 1. Organisation masters (`src/modules/organization`)

**Models** (`organization/models.py`):

| Class / table | Lines | Base | Columns | Constraints |
|---|---|---|---|---|
| `Company` / `companies` | 28-66 | `Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin`. This is the tenant root and has no `company_id`. | `code` (globally unique), `name`, `legal_name`, `gstin`, `pan`, `cin`, address fields, `state_code`, `currency` (`DEFAULT_CURRENCY`), `financial_year_start_month=4`, `timezone="Asia/Kolkata"`, `is_active`, `logo_document_id` | `ix_companies_active` |
| `Branch` / `branches` | 69-95 | `TenantMasterEntity` | `code(32)`, `name`, `gstin`, address fields, `is_active`, `is_head_office` | `uq_branches_company_id_code`, `ix_branches_company_active` |
| `Plant` / `plants` | 98-116 | `TenantMasterEntity` | `branch_id` (FK branches, RESTRICT), `code`, `name`, `address_line1`, `city`, `state`, `is_active` | `uq_plants_company_id_code`, `ix_plants_company_active` |
| `Department` / `departments` | 119-139 | `TenantMasterEntity` | `plant_id` (FK plants, RESTRICT), `parent_id` (FK departments, RESTRICT; a tree), `code`, `name`, `head_user_id` (FK users, SET NULL), `is_active` | `uq_departments_company_id_code` only |
| `FinancialYear` / `financial_years` | 142-161 | `TenantMasterEntity` | `code(16)`, `start_date`, `end_date`, `is_active`, `is_closed`, `notes` | `uq_financial_years_company_id_code`, `ix_financial_years_range` |

**Router** (`organization/router.py`, `APIRouter(prefix="/organization")` at line 60). Every route is a thin delegate that returns `ApiResponse[...]` or `PaginatedResponse[...]`:

| Endpoints | Permission | Service call |
|---|---|---|
| `GET /company`, `PATCH /company` (155-176) | `COMPANY_VIEW` / `COMPANY_MANAGE` | `CompanyService.get_company` / `update_company`. The path is singular with no id, so it is always the caller's own company. |
| `GET,POST /branches`; `GET,PATCH,DELETE /branches/{branch_id}` (184-257) | `BRANCH_VIEW` / `BRANCH_MANAGE` | `BranchService.list_branches(filters, params, sort)`, `create_branch`, `get_branch`, `update_branch`, `delete_branch(id, reason=)` |
| Plants (265-338) | `PLANT_VIEW` / `PLANT_MANAGE` | `PlantService.*` |
| Departments (346-421) | `DEPARTMENT_VIEW` / `DEPARTMENT_MANAGE` | `DepartmentService.list_departments`, `create_department`, `get_department`, `update_department`, `delete_department(id, reason=)` |
| Financial years (429-539), including `POST /{id}/close` and `POST /{id}/reopen` | `FY_VIEW = SETTINGS_VIEW` and `FY_MANAGE = SETTINGS_MANAGE` (ASSUMPTION at 72-77). Reopen uses `Depends(require_superuser)`. | `FinancialYearService.*` |

Other router details:
- Retire takes its reason as a query parameter: `ReasonQuery = Annotated[str, Query(min_length=3, max_length=255)]` (68-70).
- Filters are bound by small dependency functions, for example `department_filters(search, plant_id, parent_id, head_user_id, is_active, include_deleted)` → `DepartmentFilters` (108-123).

**Service** (`organization/service.py`):
- **Lifecycle.**
  - `MasterLifecycleState` (ACTIVE, INACTIVE, ARCHIVED) is derived from `is_active` and `deleted_at` by `lifecycle_state()` (186-198, 279-283).
  - `_lifecycle(entity, manage)` builds a `StateMachine` in which ARCHIVED is terminal (201-239).
  - Instances: `BRANCH_LIFECYCLE`, `PLANT_LIFECYCLE`, `DEPARTMENT_LIFECYCLE`, `FINANCIAL_YEAR_LIFECYCLE` (242-245). `FINANCIAL_YEAR_MACHINE` handles OPEN and CLOSED (258-276).
- **Helpers.**
  - `referenced_in_use(message, **details)` returns a `BusinessRuleError(code=ErrorCode.REFERENCED_RECORD_IN_USE)` (295-303).
  - `assert_parent_allowed(*, entity, node_id, parent_id, parent_ancestor_ids, max_depth=MAX_DEPARTMENT_DEPTH)` is pure and handles cycle and depth checks (306-340).
  - `MAX_DEPARTMENT_DEPTH = 6` is an ASSUMPTION (93-98).
- **`DepartmentService` (775-936)** is the template for a cost centre.
  - `create_department` (793-820):
    1. Inside `async with transaction(session)`, check `repo.code_taken`; a hit raises `DuplicateError`.
    2. Check references through tenant-scoped probes: `_assert_references(plant_id, head_user_id)` (930-936).
    3. Check the tree: `_assert_parent` calls `repo.ancestor_ids`, then `assert_parent_allowed` (916-928).
    4. Build `Department(**payload.model_dump())`, call `repo.add`, flush.
    5. Write `record_audit(action=CREATE, entity_type=AUDIT_ENTITY_DEPARTMENT, entity_code=..., new_values=snapshot(dept, _DEPARTMENT_AUDITED_FIELDS))`.
  - `update_department` (822-879) uses `payload.model_dump(exclude_unset=True)` and raises `NO_FIELDS_TO_UPDATE` when nothing was sent. An `is_active` change goes through `DEPARTMENT_LIFECYCLE.assert_transition` and also writes `record_status_change(reason="Activation flag changed through department update")`. It then calls `repo.touch` and writes an UPDATE audit row with old and new values.
  - `delete_department` (881-914):
    1. `assert_transition(current, ARCHIVED)`.
    2. Refuse with `referenced_in_use` if `has_live_children` or `referenced_by_users`.
    3. Set `is_active=False` and call `repo.soft_delete`.
    4. Write a DELETE audit row with `context={"reason": reason}`.

**Repository** (`organization/repository.py`):
- `_OrgRepositoryMixin` (86-125) provides two probes:
  - `_user_references(column, id)`, which is tenant-scoped and counts live users only.
  - `_warehouse_references(sql, id)`, which uses **raw SQL plus a `to_regclass` probe** (`_WAREHOUSE_BRANCH_SQL`, `_WAREHOUSE_PLANT_SQL` at 61-83). This is the pattern for probing a table owned by a module that sits *above* organisation in the dependency order.
- `DepartmentRepository` (327-440):
  - `sortable_fields` and `searchable_fields`.
  - `build_list_stmt` ends with `stmt.order_by(sort.resolve(self.sortable_fields, Department.code.asc()), Department.id.asc())`.
  - `code_taken(code, exclude_id)` includes soft-deleted rows.
  - `ancestor_ids` is a recursive CTE bounded by `MAX_HIERARCHY_WALK = 64` (364-398).
  - `has_live_children`, `plant_exists`, `user_exists`, `referenced_by_users`.

**Schemas** (`organization/schemas.py`):
- `DepartmentCreate` (328-337): `code` passes through `validate_code`, which normalises and checks the alphabet `^[A-Z0-9][A-Z0-9._\-/ ]*$`; `name` passes through `clean_name`.
- `DepartmentUpdate` (340-349): `extra="forbid"`, no `code`, every field optional.
- `DepartmentRead` (352-364): `from_attributes`.
- `DepartmentFilters` (442-448) and `ReasonRequest` (461-469).
- No schema carries `company_id`.

**Permissions** (`src/core/permissions.py`):
- `COMPANY_*`, `BRANCH_*`, `PLANT_*`, `DEPARTMENT_*`, `SETTINGS_*` (20-29).
- `PERMISSION_GROUPS["Organisation"] = ("company","branch","plant","department","settings")` (211).

**Frontend.**
- Route `/admin/organisation` is gated on `[P.COMPANY_VIEW]` only (`app/router.tsx:367`; nav at `app/navigation.ts:303-306`).
- `features/admin/organisation-page.tsx`:
  - `TabKey` union (86).
  - Local `RetireDialog` (100-174) sends `api.delete(\`${path}/${id}\`, { params: { reason } })` and invalidates `orgKeys.all`.
  - Tabs: `BranchesTab` (178-319), `PlantsTab` (323-469), `DepartmentsTab` (473-650), `FinancialYearsTab`.
  - `OrganisationPage` (910-1018) renders one tab per view right (922-941).
  - Lists use `useListQuery({ path:'/organization/departments', queryKey: orgKeys.departments, defaultSortBy:'code', staleTime: staleTimes.master })` (496-501).
- `features/admin/organisation-dialogs.tsx`:
  - Shared zod fields `codeField` (56-61, with a regex and max 32) and `nameField` (63).
  - `DepartmentFormDialog` (556-802) uses react-hook-form with `zodResolver`, resets keyed on the edited id, converts `SELECT_NONE` to null via `asId`, omits `code` on PATCH, invalidates `orgKeys.all`, toasts `success.created('Department')`, and calls `applyFieldErrors` on error.
- `features/admin/admin-types.ts`:
  - Hand-written `DepartmentRow` (191-203), **not** registered in the contract-mirror test.
  - `orgKeys` (227-234).
  - Option hooks (246-256) that wrap `hooks/use-organisation-options.ts`. That file defines keys `['organization', <kind>, 'options']`, uses `OPTION_PARAMS={limit: LOOKUP_LIST_LIMIT, is_active:true, with_total:false}` and calls `emptyOnPermissionError` (37-79).
- **There are no tests** for the organisation page or its dialogs.

### 2. Organisation dimensions already on documents (the candidate cost-centre carriers)

| Table | What it carries today | Validated in tenant? |
|---|---|---|
| `users` | `branch_id`, `plant_id`, `department_id` (`users/models.py:139-147`) | Yes: `users/service.py:1329-1330` (`repo.department_exists`) |
| `work_centers` | `plant_id`, `department_id` (`routing/models.py:118-123`) | Yes: `routing/service.py:743-751` (`_assert_organisation_refs`) |
| `purchase_requisitions` | `department_id`, `branch_id` (`procurement/models.py:331-336`), defaulted from `principal.department_id` and `principal.branch_id` (`procurement/service.py:567-568, 657-658`) | **No.** `procurement/*` never imports organisation models. See trap 3. |
| `purchase_orders` | `branch_id` (`procurement/models.py:813-815`), defaulted to `principal.branch_id` (`procurement/service.py:2146`) | **No** |
| `purchase_order_lines` | `requisition_line_id` (`procurement/models.py:1059`), so a value can be copied from the requisition | n/a |
| `production_orders` | `plant_id` (`production/models.py:244-246`) | Yes: `PlantLookupRepository.exists_in_tenant` (`production/service.py:1113-1116`) |
| `work_orders` / `production_entries` | `work_center_id`; `shift` is free text String(16) (`production/models.py:490-492, 758`) | n/a |
| `material_issues` | Nothing; tied to `production_order_id` (`production/models.py:595+`) | n/a |
| `job_work_orders` | Nothing organisational (`job_work/models.py:214+`) | n/a |
| `stock_adjustments` | Nothing organisational (`inventory/models.py:1208+`) | n/a |
| `vendor_invoices` / `vendor_invoice_lines` | Nothing organisational (`finance/models.py:487, 651`) | n/a |
| `ledger_entries` (`ImmutableLedgerEntity`) | `account_id`, `party_type`/`party_id`, `reference_*`, `financial_year_id`; **no** branch, department or cost centre (`finance/models.py:368-484`) | n/a |
| `sales_orders` | `branch_id` (`sales/models.py:159`) | Yes: `parties.branch_in_tenant` (`sales/service.py` around 1048) |

### 3. Item vocabulary in `src/modules/products`: the template for Brand and Manufacturer

**ItemCategory.**
- Model (`products/models.py:159-198`): `code(32)`, `name(255)`, `description`, `parent_id` (self-FK, RESTRICT), a materialised `path(512)`, `depth`, `is_active`.
  - Unique constraint `uq_item_categories_company_id_code`.
  - Indexes `ix_item_categories_path` (`varchar_pattern_ops`) and `ix_item_categories_parent`.
  - Checks `depth_bounded` and `parent_not_self`.
- Schemas: `CategoryCreate`, `CategoryUpdate` (with a `reparent_to_root` flag), `CategoryOut` (`schemas.py:56-89`).
- Service: `create_category` (582-629), `update_category` (632-675), `delete_category` (721-755). Delete refuses while children or items exist, then soft-deletes and audits; it takes no reason.
- Router (99-177): `GET/POST /products/categories`, `GET/PATCH/DELETE /products/categories/{id}`, gated on `CATEGORY_VIEW` / `CATEGORY_MANAGE`.

**ItemAttribute.** This is the finish, size and colour master added by migrations 0900, 0901 and 0902.

- **Enum.** `ItemAttributeType` FINISH, SIZE, COLOUR (`models.py:130-141`). The docstring says each kind is a column, so "a fourth kind is a schema change either way".
- **Model.** `ItemAttribute(TenantMasterEntity)`, `__tablename__="item_attributes"` (274-327). Columns: `attribute String(16)`, `code String(32)`, `name String(64)`, `sort_order Integer default 0`, `is_active`.
  - `UniqueConstraint("company_id","attribute","code", name="uq_item_attributes_company_id_attribute_code")`
  - `CheckConstraint(f"attribute IN ({sql_in_list(ItemAttributeType)})", name="attribute_known")`, rendered as `ck_item_attributes_attribute_known`
  - Partial index `ix_item_attributes_company_attribute (company_id, attribute, sort_order) WHERE is_active AND deleted_at IS NULL`
- **Schemas** (`schemas.py:128-161`):
  - `ItemAttributeCreate(attribute, code, name, sort_order: 0..9999 = 0, is_active=True)`; `code` passes through `normalise_code`. Note that products uses `normalise_code` (upper-case plus whitespace collapse, no alphabet check), whereas organisation uses `validate_code`.
  - `ItemAttributeUpdate(name, sort_order, is_active)`, all optional. `attribute` is deliberately absent.
  - `ItemAttributeOut(id, attribute, code, name, sort_order, is_active)`.
- **Repository.** `ItemAttributeRepository` (`repository.py:290-354`):
  - `entity_label="Item attribute"`, `searchable_fields=("code","name")`, `sortable_fields`.
  - `get_by_code(attribute, code)` uses `_scoped()`, so it **excludes soft-deleted rows**.
  - `list_page_search(params, *, attribute, search, is_active)` puts the multi-column order on the statement and passes a single clause to `list_page(..., order_by=ItemAttribute.code.asc())` (321-326; see trap 9).
  - `is_used(attribute_id)` is an EXISTS over **live items and live variants** across all six columns (328-354).
- **Service** (`service.py`):
  - Constants: `AUDIT_ITEM_ATTRIBUTE = "item_attribute"`, `ITEM_ATTRIBUTE_AUDITED_FIELDS = ["code","name","sort_order","is_active"]` (117-121).
  - `create_item_attribute` (1495-1529): duplicate check via `get_by_code`, `repo.add`, flush, CREATE audit.
  - `update_item_attribute` (1532-1577): setting `is_active=False` is **refused while `repo.is_used`** (1548-1559). The refusal is a plain `BusinessRuleError` without `REFERENCED_RECORD_IN_USE`, `is_active` is assigned directly with no StateMachine, and it is audited as UPDATE.
  - `list_item_attributes` (1580-1593).
- **Tenant and kind guard.** `_require_attribute(session, tenant, attribute_id, kind)` (1073-1116):
  1. Read through the tenant-scoped repo. Missing means `NotFoundError`, which also covers another company's id.
  2. `attribute.attribute != kind` raises `ValidationError`.
  3. `not attribute.is_active` raises `ValidationError` ("has been retired and cannot be assigned").
  - `_require_attributes(finish_id, size_id, colour_id)` (1119-1130) runs all three.
  - Callers: `update_item` (1319-1325), `create_variant` (1607), `update_variant` (1698). **Not `create_item`.** See trap 1.
- **Router** (`router.py:185-229`), gated on `ITEM_ATTRIBUTE_VIEW` / `ITEM_ATTRIBUTE_MANAGE`:
  - `GET /products/attributes?attribute&search&is_active` → `PaginatedResponse[ItemAttributeOut]`
  - `POST /products/attributes` returns 201
  - `PATCH /products/attributes/{attribute_id}`
  - There is no `GET {id}` and no `DELETE`; retirement is `PATCH is_active=false`.
- **Permissions.**
  - `ITEM_ATTRIBUTE_VIEW = "item_attribute.view"`, `ITEM_ATTRIBUTE_MANAGE = "item_attribute.manage"` (`permissions.py:66-67`).
  - Group "Item Master" = `("item","category","uom","item_attribute","price")` (214).
  - Granted to the `DESIGN_ENGINEER` preset (587-588); ADMIN and SUPER_ADMIN receive everything.
- **Migrations.**
  - `20260906_0900_item_attributes_master.py` (`4c1d70b8ea92`, the expand half):
    - `create_table` (104-145) and indexes (146-155).
    - Six `*_id` columns added and indexed, with FKs added `NOT VALID` (157-167).
    - Backfill from text, with spelling collapse logged (178-275), then `VALIDATE CONSTRAINT` (171-175).
    - A full review block covering locking, data migration and rollback (28-57).
  - `20260906_0901_drop_item_attribute_text.py` (`9b6f2ac41d38`, the contract half): drops the text columns and swaps `uq_item_variants_attribute_combo` to the nil-UUID sentinel.
  - `20260906_0902_grant_item_attribute_permissions.py` (`2f5e08c7ba61`): `array_append` of the literal strings into `roles.permissions` `WHERE is_system AND code = ANY(('SUPER_ADMIN','ADMIN'))` and only when absent (74-93); the downgrade removes them (96-103). The rationale (13-28, 61-73) is that stored role arrays never pick up new enum members, and client-owned presets are never rewritten. `_seed_roles` now refreshes `is_system` roles on every run (`src/core/seed.py:193-248`).
- **Frontend.**
  - `features/masters/masters-model.ts`: `mastersKeys.attributes` (52-64) and `ItemAttributeRow = SchemaOf<'ItemAttributeOut'>` (72), derived from the contract with no mirror registration.
  - `features/masters/attribute-dialogs.tsx`:
    - `ATTRIBUTE_KINDS` (57-61).
    - zod schema with a `(value): boolean` predicate annotation (63-76; see the CLAUDE.md TS 5.5 trap).
    - `ItemAttributeFormDialog` (94-280). The kind and code are disabled on edit. PATCH sends only `{name, sort_order, is_active}` and POST sends all fields. On success it invalidates **both** `mastersKeys.all` and `itemAttributeOptionKeys.all` (149-152).
  - `features/masters/masters-page.tsx`:
    - `TabKey` (107).
    - `AttributesTab` (813-988): `useListQuery({ path:'/products/attributes', queryKey: mastersKeys.attributes, staleTime: staleTimes.reference, filters })`. Every column sets `enableSorting:false`. The edit button is gated on `can(P.ITEM_ATTRIBUTE_MANAGE)`, and kind and status filters live in the URL.
    - `MastersPage` (1302-1387) builds tabs per view right (1308-1322).
  - `hooks/use-item-attribute-options.ts`: keys `['products','attributes','options',kind]` (31-35), `useItemAttributeOptions(kind, enabled)` (40-56), `attributeLabeller` (65-68).
  - Items:
    - `features/items/item-model.ts`: hand-written `ItemDetail` (163-207) declares `finish_id`, `size_id`, `colour_id`.
    - `features/items/item-form-dialog.tsx`:
      - zod fields (68-70); `SELECT_NONE` defaults (110-112); `valuesFrom` (140-142).
      - `buildPayload` sends `asId(...)`, which is null for "No finish" (176-178).
      - `AttributeSelect` component (208-246).
      - Hooks gated on `open && can(P.ITEM_ATTRIBUTE_VIEW)` (269-272); rendered at 419-445.
    - `features/items/item-detail-page.tsx` has `OverviewPanel` labellers (143-150) and rows "Finish/Size/Colour" (199-201).
- **Tests.**
  - Backend: `tests/integration/test_products_service.py:1040-1305`. Covers normalisation, duplicate code, the same code allowed across kinds, free renaming, an item taking a finish via `update_item`, a colour refused as a finish, an unknown id refused, a retired value refused, refusal to retire a value in use (items and variants), clearing a variant attribute, and listing with ordering. **Every assignment test goes through `update_item`, none through `create_item`.**
  - Frontend: `features/masters/attribute-dialogs.test.tsx` (harness `renderScreen`, `ok`, `fail`, `fetch.requestFor`) and `features/items/items-page.test.tsx:381-431` (POST sends `finish_id` and null for unset fields). **`masters-page.test.tsx` has no Attributes-tab test** (grep shows no "attribute" assertions).

### 4. Item model fields (`products/models.py:330-540`)

- **Identity and classification:** `code(64)`, `name`, `description`, `item_group` (reporting only), `category_id`.
- **Units:** `base_uom_id`, `purchase_uom_id`, `sale_uom_id`.
- **Tax:** `hsn_code`, `gst_rate` Numeric(9,4).
- **Prices:** `standard_cost` (Money; serialised only for `item.cost_view`), `sale_price` and `dealer_price` (UnitPrice, at 370-375).
- **Attributes:** `finish_id`, `size_id`, `colour_id` (FK `item_attributes`, RESTRICT, indexed; 383-391).
- **Capability flags:** nine `can_*` flags.
- **Planning:** `production_type`, `lead_time_days`, `reorder_level`, `reorder_quantity`, `min_order_quantity`, `safety_stock`, `shelf_life_days`.
- **Tracking and other:** `is_batch_tracked`, `is_serial_tracked`, `default_warehouse_id`, `is_active`.

Item audit fields are listed in `ITEM_AUDITED_FIELDS` (`service.py:128-164`). The list projection is `ITEM_SUMMARY_COLUMNS`, applied with `load_only(..., raiseload=True)` (`repository.py:100-119, 501`). `item_response` builds the detail payload by `getattr` over `ItemDetail.model_fields` (509-526).

### 5. Brand and manufacturer: confirmed absent, with the nearest existing fields

- `item_vendor_links.vendor_item_code` / `vendor_item_name` (`models.py:691-692`) hold the *vendor's* part number, not a manufacturer part number. The model comment says the code is "printed on the PO", but nothing prints it: grep finds it only in schemas, the service and demo data, and there is no PO PDF renderer in `src/workers/tasks/reports.py`.
- `stock_batches.manufactured_on` is a date (`inventory/models.py:947`).
- `vendors.vendor_type` covers RAW_MATERIAL_SUPPLIER, COMPONENT_SUPPLIER, PACKING_MATERIAL_SUPPLIER and JOB_WORK, with no manufacturer flag (`vendors/models.py:61-76`).
- "Specification masters" (proposal p.3) are only partly covered: ItemAttribute holds finish, size and colour; `rfq_lines.specification` is free text (`procurement/models.py:589`); QC templates are inspection specifications.

### 6. Pricing

- **`PriceType`** (`products/models.py:101-106`): SALE, DEALER, DISTRIBUTOR.
  - `customers.price_list_type` carries a DB CHECK `ck_customers_price_list_type_known` (`customers/models.py:229-232`; initial migration 796-797).
  - `item_prices.price_type` has **no** DB CHECK; only the Pydantic enum constrains it.
- **`ItemPrice`** (`item_prices`, `products/models.py:615-668`): `item_id` (CASCADE), `variant_id` (NULL means all variants), `price_type`, `currency` (`DEFAULT_CURRENCY`), `price` (UnitPrice, 6 decimal places), `min_quantity` (slab floor), `valid_from`, `valid_to` (NULL means open-ended), `is_active`, `notes`.
  - It has **no UoM, no customer and no brand**.
  - Index `ix_item_prices_lookup(company_id,item_id,price_type,valid_from) WHERE is_active AND deleted_at IS NULL`.
  - Checks `price_non_negative`, `min_quantity_non_negative`, `validity_window_ordered`.
- **`ItemVariant.price_delta`** (571-573): deltas, not absolute prices.
- **Service** (`products/service.py`):
  - `create_price` (1786-1850) refuses an overlap among rows with the same item, price type, `min_quantity` and variant (`ItemPriceRepository.overlapping`, `repository.py:704-734`).
  - `update_price` (1853-1906) ignores nulls, so `valid_to` cannot be cleared.
  - `delete_price` soft-deletes.
  - `select_price_row(rows, quantity, *, variant_id)` (467-501).
  - `ITEM_MASTER_FALLBACK_FIELD` ASSUMPTION (1775-1783).
  - `resolve_price(...)` (1944-2018).
- **Endpoints** (`products/router.py:540-636`):
  - `GET/POST /products/items/{id}/prices`, `PATCH/DELETE /products/prices/{id}` (`PRICE_VIEW` / `PRICE_MANAGE`).
  - `GET /products/items/{id}/resolved-price?price_type&quantity&variant_id&on_date` (`PRICE_VIEW`) returns `ResolvedPrice{item_id, variant_id, price_type, quantity, on_date, price, currency, source: PRICE_LIST|ITEM_MASTER, price_id, min_quantity}` (`schemas.py:589-604`). **No frontend calls it.**
- **Item-vendor links** (`ItemVendorLink`, 671-744):
  - Columns: `vendor_id`, `vendor_item_code`, `vendor_item_name`, `is_preferred` (partial unique index, one preferred per item; demote before promote, 2052-2057), `lead_time_days`, `min_order_quantity`, `order_multiple`, `last_purchase_price`, `last_purchase_date`, `currency`, `is_active`.
  - Endpoints: `GET/POST /products/items/{id}/vendors` and `PATCH/DELETE /products/item-vendors/{id}` (`router.py:644-702`).
  - **The frontend only lists them** (`item-detail-page.tsx:777-831`). No dialog creates or edits a link.
- **Customers' pricing-related fields** (`customers/models.py:100-239`):
  - `customer_type` CUSTOMER, DEALER, DISTRIBUTOR or OEM (classification only, 66-78).
  - `price_list_type` (default SALE, 122-124; deliberately independent of type).
  - `credit_limit` (NULL means unlimited, 0 means cash only; ASSUMPTION at `customers/service.py:99-106`; `docs/assumptions.md` §1).
  - `credit_days` (the payment term, 131), `outstanding_amount` (finance-owned).
  - `is_blocked` with reason.
  - **There is no discount %, price tier, priority or customer-specific price.** Priority exists on sales orders (1-9, `sales/models.py:66-76`).
  - Writing a DEALER or DISTRIBUTOR account needs `dealer.manage` in addition to the customer permission (`assert_channel_write_allowed`, `customers/service.py:533-546`).
  - `GET /customers/dealers` (`require_any_permission(DEALER_VIEW, CUSTOMER_VIEW)`, router 130-133) feeds a read-only Dealers screen.
- **How a sales line gets its default price** (`sales/service.py`):
  1. `create_order` (1023) sets `order_date = payload.order_date or today()` (1062).
  2. `_build_pricing_context` (709-764) does, for the whole order:
     - `ItemLookupRepository.load(item_ids)` (`sales/repository.py:97-134`; explicit column list in `SellableItem` 54-80)
     - `items_repo.variants`
     - `PriceListRepository.candidates_for_items(item_ids, price_type=PriceType(customer.price_list_type), variant_ids, on_date)` (185-219), one statement for all lines
  3. `price_line` (627-701): a payload `unit_price` counts as an override. It needs `PRICE_MANAGE` (`PermissionDeniedError`); below `standard_cost` it sets `order.requires_approval` (`is_below_cost` at 153-167). Without an override it calls `resolve_unit_price` (592-624), which **re-implements** the fallback beside `products.resolve_price` while sharing `select_price_row`.
  4. `compute_line_amounts` (around 286-350) produces gross, discount (% or flat, mutually exclusive), taxable, then CGST and SGST or IGST.
- `OrderPartyRepository.customer_price_type(customer_id)` (`sales/repository.py:340-348`) exists but **nothing calls it**.

### 7. Exactly how to compute "the price this dealer pays for this item today"

1. **Channel.** `pt = customers.price_list_type` for that dealer. This is not `customer_type`; a DEALER account sits on SALE unless someone set DEALER.
2. **Date.** `D = today()`, which is the **UTC** date (`src/core/clock.py:23-25`; a row whose `valid_from` is tomorrow takes effect at 05:30 IST). A sales order uses `order_date`.
3. **Candidate rows.**
   ```sql
   item_prices WHERE company_id = :tenant AND item_id = :item AND price_type = :pt
     AND is_active AND deleted_at IS NULL
     AND valid_from <= :D AND (valid_to IS NULL OR valid_to >= :D)
     AND (variant_id IS NULL OR variant_id = :variant)
   ```
   For the plain item, only `variant_id IS NULL` rows qualify. Windows are inclusive at both ends.
4. **Pick the row** with `select_price_row(rows, Q, variant_id)`:
   - Eligible rows have `min_quantity <= Q`.
   - Choose the maximum of `(1 if row is for this variant else 0, min_quantity, valid_from, id)`.
   - So the variant-specific row wins first, then the highest slab reached, then the newest window, with the UUIDv7 id as the tiebreak.
5. **If a row was chosen:** `price = row.price + (variant.price_delta if variant and row.variant_id is None else 0)`, rounded with `to_unit_price` to 6 decimal places. `currency = row.currency`, `source = "PRICE_LIST"`.
6. **Otherwise, fall back:**
   - DEALER uses `items.dealer_price`; SALE uses `items.sale_price`. Add `variant.price_delta` if a variant is given.
   - `currency` is the literal `"INR"` (`products/service.py:2014`) and `source = "ITEM_MASTER"`.
   - DISTRIBUTOR raises `BusinessRuleError("No DISTRIBUTOR price is configured ...")`.
7. **Net price on an order line:**
   - `gross = to_money(Q × price)`, minus the line discount (a manual % or flat amount; no customer default exists), gives `taxable`.
   - GST uses `items.gst_rate`, split CGST and SGST (each rounded) when the seller branch state code equals the shipping state code, otherwise IGST.

Via the API, the same calculation is `GET /api/v1/products/items/{id}/resolved-price?price_type=<dealer's price_list_type>&quantity=Q`. The caller must look up `price_list_type` first, because the endpoint takes no `customer_id`.

### 8. Documentation

- `docs/database.md` §2 module map (organisation and products tables) and §3 "Item master" with an "Item attributes" subsection (lines 81-150). It lists the three rules: nothing branches on a value; the kind is immutable; assignment is validated for tenant and kind. The "Other required columns" list is at 133-137.
- `docs/assumptions.md`:
  - §1 credit limit semantics, §8 price-override authority, §9 GST place of supply, §13 financial year and numbering, §32 customer charges as receivable journals.
  - Open questions 3 (multi-currency) and 5 (vendor price visibility).
  - **No section covers item attributes, the price-list fallback (`ITEM_MASTER_FALLBACK_FIELD`), or any organisation ASSUMPTION** (`MAX_DEPARTMENT_DEPTH`, financial-year permissions, `MAX_FINANCIAL_YEAR_DAYS`), even though the code marks each with `ASSUMPTION:`.

---

## Missing vs proposal

**Gap 5: cost-centre master.**
- No `cost_centres` table, permission, endpoint, screen or `cost_centre_id` column on any document.
- No cost-centre reporting: ledger entries carry only account, party, reference and financial year.
- Departments are described as "for cost attribution" but are referenced only by `users`, `purchase_requisitions` and `work_centers`, and nothing aggregates by department.

**Gap 6: brand and manufacturer masters.**
- No `brands` or `manufacturers` table, and no `items.brand_id`, `manufacturer_id` or MPN.
- No `item_vendor_links.manufacturer_id` or `manufacturer_part_number`.
- No brand filter on item search (`ItemFilters`, `schemas.py:390-441`) and no Masters tab.
- "Specification masters" are only partly covered, as described in section 5 above.

**Dealer pricing for a dealer catalogue** (proposal "Dealer pricing & discount management").
- What exists is listed in section 6.
- Missing:
  1. A customer-specific price or price override (no `customer_id` on `item_prices`).
  2. Dealer discount % by brand, category or item (no table; `customers` has no discount).
  3. MRP or list price. A new `PriceType` member would need a CHECK migration on customers and a `ITEM_MASTER_FALLBACK_FIELD` entry.
  4. A bulk catalogue or price-preview endpoint. `resolved-price` handles one item and takes `price_type`, not a customer.
  5. A dealer principal. `users` has only `vendor_id` (`users/models.py:150`; `Principal.vendor_id`, `src/core/deps.py:83`). Vendor scoping is applied in `ItemRepository.build_search_statement` (`repository.py:532-535`), and dealer scoping has no equivalent.
  6. Price provenance on sales lines. `price_source` is computed (`PricedLine.price_source`, `sales/service.py:583`) and not persisted.
  7. UoM-aware prices (see trap 13).
  8. Editing item-master `sale_price` and `dealer_price` from the UI. The item form has neither field (`item-form-dialog.tsx:63-100, 167-199`), yet the detail page shows both (`item-detail-page.tsx:188-189`).

---

## Patterns to copy

| New piece | Closest precedent | Conventions to reuse exactly |
|---|---|---|
| `Brand`, `Manufacturer` masters (products) | **ItemAttribute** (model 274-327, schemas 128-161, repo 290-354, service 1495-1593 and 1073-1130, router 185-229, migrations 0900 and 0902, `attribute-dialogs.tsx`, `AttributesTab`, `use-item-attribute-options.ts`, tests 1040-1305) | See the bullets below this table. |
| `CostCentre` master (organisation) | **Department** (model 119-139, schemas 328-448, repo 327-440, service 775-936, router 346-421, `DepartmentFormDialog`, `DepartmentsTab`, `RetireDialog`) plus the `_WAREHOUSE_*_SQL` raw probes for retire guards | See the bullets below this table. |
| A document carrying a reference to a master | Sales: `parties.branch_in_tenant` (`sales/service.py`, around 1048). Routing: `_assert_organisation_refs` (`routing/service.py:743-751`). Products: `ItemRepository._tenant_row_exists` (`repository.py:414-430`). | Check tenant and liveness before writing, and report `NotFoundError` for a foreign id. **Never** copy procurement's unvalidated `department_id` and `branch_id` (trap 3). |
| Batch validation inside a posting | `LedgerAccountRepository.existing_ids` (`finance/repository.py:292-306`), called once per journal in `post_journal` | One `IN (...)` probe per journal, never a query per leg. |
| Grant a new permission to deployed system roles | `20260906_0902_grant_item_attribute_permissions.py` | Literal strings; `is_system` roles only; idempotent `NOT (:p = ANY(permissions))`; the downgrade removes them. |
| Expand migration with FKs on a large table | `20260906_0900` (157-175) | `ADD COLUMN NULL`, then `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT`, plus the section-67 review docstring. **Add the actor FKs** that it forgot (trap 5). |
| Option hook for pickers | `hooks/use-item-attribute-options.ts` and `hooks/use-organisation-options.ts` | One query key per list, `LOOKUP_LIST_LIMIT`, `is_active:true`, `with_total:false`, `emptyOnPermissionError`, `staleTimes.reference`. Put it in `hooks/`, not in each screen. |
| Dialog test | `features/masters/attribute-dialogs.test.tsx` | `renderScreen(<Dialog/>, { permissions, routes: {'POST /path': ok(row)} })`; read the request body with `fetch.requestFor`; map 409/422 fields through `fail(...)`. |
| Integration test | `tests/integration/test_products_service.py:1040-1305` and `test_organization_service.py` | Use the `session`, `tenant`, `company`, `other_company` fixtures and `tests/seed_support` (ORM only, never raw INSERT). |
| Pure tree tests | `tests/unit/test_organization_hierarchy.py` | Reuse for the cost-centre parent rules. |

**Conventions for the Brand and Manufacturer masters** (copied from ItemAttribute):
- `class Brand(TenantMasterEntity)`.
- `UniqueConstraint("company_id","code", name="uq_brands_company_id_code")`.
- A partial picker index `WHERE is_active AND deleted_at IS NULL`.
- `normalise_code` on `code`. `validate_code` is stricter if the alphabet matters; pick one and record it.
- Update schemas with "None means leave alone" fields.
- `entity_label="Brand"`.
- `get_by_code` must **include soft-deleted rows** if a DELETE route is added.
- `list_page(params, stmt=..., order_by=<one clause>)`.
- `is_used(id)` as an EXISTS over live rows.
- `_require_brand(session, tenant, brand_id)`: tenant-scoped `.get()` (`NotFoundError`), then `is_active` (`ValidationError`).
- Audit constants `AUDIT_BRAND="brand"` and `BRAND_AUDITED_FIELDS`.
- `record_audit` for CREATE and UPDATE.
- Routes `GET/POST /products/brands` and `PATCH /products/brands/{id}`, with `require_permission(Permission.BRAND_VIEW|BRAND_MANAGE)` spelled literally inside the decorator (the gates test parser reads `Permission.X` there).
- Prefer the stricter organisation lifecycle (StateMachine plus `record_status_change`, `referenced_in_use`) over ItemAttribute's direct `is_active` assignment.

**Conventions for the CostCentre master** (copied from Department):
- `TenantMasterEntity`, `code String(32)`, `name String(255)`, a self-FK `parent_id` (RESTRICT, indexed), `CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self")`, `uq_cost_centres_company_id_code`, `ix_cost_centres_company_active`.
- Schemas use `validate_code` and `clean_name`; the update schema has `extra="forbid"` and no `code`.
- Repository: `build_list_stmt` with `sort.resolve(..., CostCentre.code.asc())` plus an id tiebreak; `code_taken` including deleted rows; `ancestor_ids` CTE; `has_live_children`.
- Service: `COST_CENTRE_LIFECYCLE = _lifecycle("Cost centre", Permission.COST_CENTRE_MANAGE)` and `assert_parent_allowed(entity="cost centre", ...)`.
- Delete takes `reason` (`ReasonQuery`), refuses through `referenced_in_use`, soft-deletes, and audits with `context={"reason":...}`.
- Routes `/organization/cost-centres` with `PaginatedResponse[...]` and `ApiResponse[...].ok(x, "Cost centre created")`.

---

## Integration points

**Brand and Manufacturer.**

- `products/models.py`:
  - `Item.brand_id`, `Item.manufacturer_id` (FK RESTRICT, nullable, `index=True`), `Item.manufacturer_part_number String(64)`.
  - Optionally `ItemVendorLink.manufacturer_id` and `manufacturer_part_number` (one maker and MPN per source).
  - Optionally `Brand.manufacturer_id` (brand owner).
  - No variant columns: brand does not vary by finish.
- `products/schemas.py`:
  - `ItemCreate`, `ItemUpdate`, `ItemDetail` get the new fields (they flow automatically through `item_response`).
  - If `ItemSummary` gains `brand_id`, `ITEM_SUMMARY_COLUMNS` must gain it too (trap 8).
  - `ItemFilters` and `item_filters()` get `brand_id` and `manufacturer_id`.
  - `VendorLinkCreate`, `VendorLinkUpdate`, `VendorLinkOut` get the MPN fields.
- `products/service.py`:
  - `create_item` and `update_item` call `_require_brand` and `_require_manufacturer`, and `create_item` should also call `_require_attributes`.
  - `ITEM_AUDITED_FIELDS` gains the new fields.
  - `link_vendor` and `update_vendor_link` validate the manufacturer.
- `products/repository.py`: `build_search_statement` filters on brand. Keep the CLAUDE.md empty-list rule: a filter on an empty set must return nothing.
- `src/core/demo_data/masters.py`: `_seed_brands`, keyed on code, following `_seed_item_attributes` (1019-1051).
- Frontend: Masters tabs, `hooks/use-brand-options.ts`, the item form selects, item detail labels, an items-list brand filter, and an MPN column in the item-detail `VendorsPanel`. There is no link-edit UI today.
- A future dealer catalogue reads `items.brand_id` together with `can_sell`, `is_active` and price resolution.

**Cost centre** (organisation sits below every consumer, so all of them may FK to it):

- **Organisation.**
  - Add `departments.cost_centre_id` as the default for documents raised by that department's users. `Principal.department_id` already exists (`deps.py:88`).
  - The cost-centre retire guard must probe `departments`, `work_centers`, child cost centres and open documents through raw SQL plus `to_regclass`, following the pattern at `organization/repository.py:61-125`.
- **Routing.** Add `work_centers.cost_centre_id` (machine-hour pools) and validate it in `_assert_organisation_refs`.
- **Procurement.**
  - Add `purchase_requisitions.cost_centre_id`, and optionally the same on lines. Default precedence: explicit value, then the requester's department default.
  - `purchase_orders.cost_centre_id` and `purchase_order_lines.cost_centre_id`, copied through `requisition_line_id` and by the MRP and quotation conversion paths (`create_requisition_from_mrp` passes `department_id=None` at `procurement/service.py:617`).
  - Purchase returns should credit the same cost centre.
- **Finance.**
  - `vendor_invoices` and `vendor_invoice_lines` get `cost_centre_id`, defaulted from the PO line.
  - `_post_invoice_journal` (`finance/service.py:2250-2305`) posts **one** `PURCHASE_EXPENSE` debit for the subtotal today. It must split that leg per line cost centre.
  - `JournalLine` (500-516) gains `cost_centre_id`.
  - `post_journal` (1158-1271) validates the ids in tenant in one batch and allows them only on INCOME and EXPENSE accounts (`LedgerAccountType`, `finance/models.py:124-131`). It writes `LedgerEntry.cost_centre_id`, a new nullable column on an immutable table, so it can only be set at posting time.
  - `reverse_journal` (1274-1324) must copy `cost_centre_id` from each original leg.
  - New index `(company_id, cost_centre_id, entry_date)`.
- **Production.** Add `production_orders.cost_centre_id`, defaulting from plant or work centre. `material_issues` inherit through `production_order_id`, so no column is needed there. Production costing (`production/service.py:3326-3367`) can roll up by work-centre cost centre.
- **Job work.** Add `job_work_orders.cost_centre_id` (charges such as plating or PVD). It is consumed by `job_work_charges_for_period` (`job_work/service.py:1153`).
- **Inventory.** Add `stock_adjustments.cost_centre_id` for write-offs. **Do not** add it to `inventory_transactions`: that changes the `post_movement` signature. Derive it through `reference_type` and `reference_id` instead.
- **Reports.** A new cost-centre expense statement that aggregates `ledger_entries` by cost centre and account over a period, using the reports catalogue.

**Dealer pricing.** Any new rule (customer override, brand or category discount, MRP) must be applied in **all three** of these places, or the paths must first be merged into one function in `products/service.py`:
- `products.service.resolve_price` (1944-2018)
- `sales.service.resolve_unit_price` (592-624)
- `sales.repository.ItemLookupRepository.load` (explicit columns; add `brand_id` and `category_id` there if discounts depend on them)

Otherwise "sales and the price-list screen disagree" (the docstring at 600-607).

---

## Traps and constraints

1. **`create_item` never validates `finish_id`, `size_id` or `colour_id`** (`products/service.py:1133-1215`; the values are stored directly at 1184-1186). `POST /products/items` accepts another tenant's attribute id, or a colour in `finish_id`, which is exactly the CLAUDE.md class "A foreign key to a UUID is not a tenant check". Tests only exercise `update_item`. Do not copy this when adding brand and manufacturer; fix it in the same change.
2. **Department and plant retire guards are incomplete.**
   - `delete_department` checks children and users only (`organization/service.py:887-897`), not `work_centers.department_id` or `purchase_requisitions.department_id`.
   - `delete_plant` (726-740) does not check `work_centers` or `production_orders`.
   - A cost-centre retire guard must probe every carrier table, using raw SQL plus `to_regclass` because those modules sit above organisation.
3. **Procurement trusts `department_id` and `branch_id` from the payload** (`procurement/service.py:657-658, 723-733, 2146`). Nothing checks tenant ownership. Any `cost_centre_id` placed beside them must be validated, and these two should be fixed too.
4. **Retiring a value must not break posted history.** `post_journal` accepts only *active* accounts (`finance/repository.py:300-304`), so reversing a journal on a deactivated account fails. Decide explicitly whether a retired cost centre may appear on a reversal leg. Follow the row one step further (CLAUDE.md): retiring a cost centre must not make its journals irreversible.
5. **Migration drift in `4c1d70b8ea92`.** `created_by_id` and `updated_by_id` were created without the `users` FK that `ActorMixin` declares (migration lines 125-126, compared with `fk_items_created_by_id_users` in the initial schema). Copying that `create_table` verbatim repeats the drift. Include `ForeignKeyConstraint` for both actor columns, and keep `company_id` FK to companies with RESTRICT plus `ix_<table>_company_id`.
6. **A new permission reaches no deployed role by itself.**
   - `_seed_roles` refreshes only `is_system` roles, and only when the seeder runs. A grant migration modelled on `2f5e08c7ba61` is required.
   - Client-owned presets (DESIGN_ENGINEER, ACCOUNTS and the rest) are not updated on existing deployments.
   - `tests/unit/test_role_presets_reach_roles.py` fails unless the new resource is in `PERMISSION_GROUPS` (44-58).
7. **Routes and navigation must list each new tab's view right.**
   - `/masters` is mounted on `canAny(CATEGORY_VIEW, UOM_VIEW, PRICE_VIEW)` (`router.tsx:349-352`; `navigation.ts:124-128`), which already omits `ITEM_ATTRIBUTE_VIEW`. Its empty-state text names only three rights (`masters-page.tsx:1349`).
   - `/admin/organisation` requires `COMPANY_VIEW` only (367), so a holder of `cost_centre.view` alone would never reach a tab.
   - The permission-gates test requires each GET on a multi-right route to be gated by its own right inside the screen.
8. **`load_only(..., raiseload=True)` on item lists** (`repository.py:501`). Adding `brand_id` to `ItemSummary` without adding it to `ITEM_SUMMARY_COLUMNS` makes every item list (offset and keyset) raise, which shows as a 500. The frontend `ItemRow` mirror is checked against `ItemSummaryWithCost`.
9. **Ordering pitfalls.**
   - `list_page(order_by=...)` takes **one** clause; a tuple raises (comment at `repository.py:321-326`).
   - Never write `clause or default`; use the `x if x is None else y` form (`repository.py:549-555`).
   - Audit and ledger ordering must be `(created_at, id)`. The organisation test helper `_audit_rows` orders by `created_at` only (`tests/integration/test_organization_service.py:81-90`) and asserts order at 649, so do not copy it.
10. **Frontend contract mirrors.**
    - `tests/unit/test_frontend_contract_types.py` (`MIRRORS` at 66-76) checks that hand-written `ItemDetail` declares nothing beyond `ItemDetailWithCost`. Add the backend field first.
    - `test_every_hand_written_mirror_of_an_item_shape_is_registered` (143-174) fails for any new `export interface` in `item-model.ts`. Use `export type BrandRow = SchemaOf<'BrandOut'>` instead.
    - Organisation row types are hand-written and unregistered; prefer `SchemaOf<'CostCentreRead'>` for new rows.
11. **Other meta-tests that will judge new code.**
    - `test_frontend_permission_catalogue.py`: the enum and `lib/permissions.ts` must match in both directions.
    - `test_frontend_permission_gates.py`: each mutation's `Permission.X` (the first one in the decorator) must be named as `P.X` in the calling feature folder or route table, so keep the literal `Permission.` in decorators. Aliases like `FY_MANAGE` hide routes from the parser.
    - `test_schema_identifier_length.py`: 63-byte names, with the `ck_%(table)s_%(name)s` convention from `src/core/models.py:35-41`.
    - `test_state_machine_entity_names.py`: human names such as "Cost centre" and "Brand".
    - `test_repository_entity_label.py` and `test_entity_names.py`.
    - `test_status_badge_catalogue.py`: any new `*Status` or `*State` StrEnum value needs a badge entry.
    - `test_e2e_sweep_routes.py`: any new **static route** must be added to `browser-sweep.mjs`, `a11y-sweep.mjs`, `responsive-sweep.mjs` and `permission-sweep.mjs` (`tests/e2e/scripts/`). Tabs avoid this.
    - `test_frontend_constants.py`: no literal `limit` values.
    - `test_frontend_field_errors.py`: use the single `applyFieldErrors`.
    - `make contract-check`: regenerate `openapi.json`, `apps/web/src/types/openapi.ts` and `postman_collection.json`.
12. **Line discounts bypass price-override authority.** `discount_percentage` and `discount_amount` (up to 100% or the full gross, `sales/service.py:319-330`; schema `DiscountPercentage` 0-100) need neither `price.manage` nor below-cost approval. Only a `unit_price` override is gated (`sales/service.py:663-671`; `docs/assumptions.md` §8). A dealer-discount feature must close this, not add a second bypass.
13. **Pricing ignores units of measure.** `item_prices` has no unit. A sales line may take any tenant unit (only existence is checked, `sales/service.py:742-747`, and the UI offers every unit, `sales-order-line-dialog.tsx:231-247`). The quantity is compared with `min_quantity` and multiplied by the price without conversion. A catalogue must state its pricing unit.
14. **Currency and date edges.** The fallback currency is the literal `"INR"` (`products/service.py:2014`) rather than `DEFAULT_CURRENCY` or the company currency. "Today" is the UTC date.
15. **Adding a `PriceType` member** (for example MRP) requires all of these: a `ITEM_MASTER_FALLBACK_FIELD` entry (otherwise a `KeyError` gives a 500 in both resolvers), a migration replacing `ck_customers_price_list_type_known`, and updates to the frontend `PRICE_TYPES` (`masters-model.ts:112` and `features/customers/customer-types.ts:29-31`).
16. **Products masters do not route `is_active` through a StateMachine.** ItemAttribute and UoM assign it directly and audit it as UPDATE (`service.py:1548-1559, 846-852`). CLAUDE.md requires status changes through `assert_transition`; the organisation masters do it correctly. Use the organisation pattern for new masters.
17. **A retired value disappears from the picker.** `attributeLabeller` prints the raw id when a value is missing from the active list (`use-item-attribute-options.ts:65-68`). Retirement is refused while a value is in use, which keeps this rare. Keep that refusal for brand and manufacturer.
18. **The department parent picker uses only the current page** (`siblings={list.data}`, `organisation-page.tsx:~641`). A cost-centre tree needs a full reference list, as `useCategoryReference` does (`masters-model.ts:178-190`).
19. **Undocumented ASSUMPTIONs.** Record new ones in `docs/assumptions.md`; the next section is §39. The pricing fallback and organisation ASSUMPTIONs are not recorded there yet.
20. **CLAUDE.md test and data traps that apply here.**
    - Build fixtures through the ORM (`tests/seed_support`), never `INSERT`.
    - Never hand-write another module's table in a test.
    - Never call `session.rollback()` in a service.
    - Read ids into plain variables before an expected failure inside `transaction()`.
    - Demo seed idempotence must be keyed on code, never on clock-derived values (`_price_exists`, `demo_data/masters.py:1229-1255`).

---

## Files a change here will touch

**Brand and Manufacturer**

- Backend:
  - `src/modules/products/models.py`: `Brand`, `Manufacturer`; columns on `Item` and `ItemVendorLink`; `__all__`.
  - `src/modules/products/schemas.py`
  - `src/modules/products/repository.py`: `BrandRepository`, `ManufacturerRepository`, item filters, `ITEM_SUMMARY_COLUMNS` if listed.
  - `src/modules/products/service.py`
  - `src/modules/products/router.py`
  - `src/core/permissions.py`: enum, `PERMISSION_GROUPS["Item Master"]`, presets.
  - `src/core/demo_data/masters.py`
- Migrations:
  - `migrations/versions/2026xxxx_brand_manufacturer_masters.py` (down from `2f5e08c7ba61`)
  - `..._grant_brand_manufacturer_permissions.py`
- Tests:
  - `tests/integration/test_products_service.py`
  - `tests/unit/test_products_reference_probes.py` (if probes move to `ItemRepository`)
  - Contract-mirror registrations only if hand-written types are used.
- Frontend:
  - `lib/permissions.ts`
  - `app/router.tsx` and `app/navigation.ts` (the `/masters` rights)
  - `features/masters/masters-model.ts`, `features/masters/masters-page.tsx`
  - New `features/masters/brand-dialogs.tsx` and `features/masters/manufacturer-dialogs.tsx`, each with a `.test.tsx`
  - `features/masters/masters-page.test.tsx`
  - New `hooks/use-brand-options.ts` (or extend the attribute hook file)
  - `features/items/item-model.ts`, `item-form-dialog.tsx`, `item-detail-page.tsx`, `items-page.tsx`
  - `features/items/items-page.test.tsx`, `features/items/item-detail-page.test.tsx`
  - `types/openapi.ts` (generated)
- Root: `openapi.json`, `postman_collection.json`.
- Docs: `docs/database.md` §2 and §3, `docs/assumptions.md`, `docs/api.md` endpoint map, `docs/handbook/content.json`, `tests/e2e/scripts/capture-screens.mjs`.

**Cost centre**

- Backend:
  - `src/modules/organization/models.py`, `schemas.py`, `repository.py`, `service.py`, `router.py`
  - `src/core/permissions.py`
- Phase 2 carriers:
  - `src/modules/organization/*`: `departments.cost_centre_id`
  - `src/modules/routing/models.py`, `schemas.py`, `service.py`
  - `src/modules/procurement/models.py`, `schemas.py`, `service.py`, `repository.py`
  - `src/modules/finance/models.py`, `schemas.py`, `service.py` (`JournalLine`, `post_journal`, `reverse_journal`, `_post_invoice_journal`), `repository.py`
  - `src/modules/production/*`, `src/modules/job_work/*`, `src/modules/inventory/*` (adjustments)
  - `src/modules/reports/*`
- Migrations:
  - The cost-centre table migration and its grant migration.
  - One expand migration for the carrier columns, using NOT VALID FKs on `ledger_entries` and other large tables.
- Tests:
  - `tests/unit/test_organization_hierarchy.py`-style tree tests
  - `tests/integration/test_organization_service.py`
  - `tests/integration/test_finance_service.py`, `tests/integration/test_procurement_service.py`
  - API tests under `tests/api/`
- Frontend:
  - `lib/permissions.ts`
  - `features/admin/admin-types.ts` (`orgKeys.costCentres`, or a `SchemaOf` row)
  - `hooks/use-organisation-options.ts` (`useCostCentreOptions`)
  - `features/admin/organisation-page.tsx`, `features/admin/organisation-dialogs.tsx`
  - New `features/admin/organisation-page.test.tsx`
  - Pickers in the procurement, finance, production and job-work forms
  - `app/router.tsx` and `app/navigation.ts` (add `COST_CENTRE_VIEW` to `/admin/organisation` rights)
  - `tests/e2e/scripts/permission-sweep.mjs` only if the route's rights change in a way that affects the named right.
- Docs: `docs/database.md` §2 and §8, `docs/assumptions.md`, `docs/api.md`, the handbook.

---

## Conclusion: build recipe

### A. Brand and Manufacturer masters (products module)

**Design decision.** Use two separate tables, not new `ItemAttributeType` kinds.
- A brand is not a physical attribute and does not belong on `item_variants`.
- A manufacturer needs extra fields (legal name, country, website), and `ItemAttribute.name` is only `String(64)`.
- `ItemAttribute.is_used` and the `attribute_known` CHECK would both need rewriting anyway.
- A manufacturer is not modelled as a vendor. You often buy one maker's cylinders through a distributor, and vendors carry payables and portal semantics.

**Steps:**

1. **`products/models.py`**
   - `class Manufacturer(TenantMasterEntity)` with `__tablename__="manufacturers"`:
     - `code String(32)`, `name String(255)`, `legal_name String(255)|None`, `country String(80) default "India"`, `website|None`, `notes Text|None`, `is_active` (`server_default true`)
     - `UniqueConstraint("company_id","code", name="uq_manufacturers_company_id_code")`
     - Partial index `ix_manufacturers_company_active (company_id, name) WHERE is_active AND deleted_at IS NULL`
   - `class Brand(TenantMasterEntity)` with `"brands"`:
     - `code String(32)`, `name String(120)`, `description Text|None`, `manufacturer_id` (FK manufacturers, RESTRICT, nullable, indexed; the brand owner), `sort_order Integer=0`, `is_active`
     - `uq_brands_company_id_code` and a partial picker index
   - `Item`: `brand_id` and `manufacturer_id` (FK RESTRICT, nullable, `index=True`), `manufacturer_part_number String(64)|None`. Optionally add `Index("ix_items_company_brand", "company_id", "brand_id", postgresql_where=text("deleted_at IS NULL"))` for catalogue browsing.
   - `ItemVendorLink`: `manufacturer_id` (FK RESTRICT, nullable) and `manufacturer_part_number String(64)|None`.
2. **`products/schemas.py`**
   - `BrandCreate`, `BrandUpdate` (no `code`), `BrandOut`; the same three for Manufacturer. Use `normalise_code`, or `validate_code` with the choice recorded.
   - Add the fields to `ItemCreate`, `ItemUpdate`, `ItemDetail`, and optionally `ItemSummary` together with `ITEM_SUMMARY_COLUMNS`.
   - Add `brand_id` and `manufacturer_id` to `ItemFilters` and to `item_filters()` (`Query`).
   - Add the MPN fields to `VendorLinkCreate`, `VendorLinkUpdate`, `VendorLinkOut`.
3. **`products/repository.py`**
   - `BrandRepository` and `ManufacturerRepository`: `entity_label`, `searchable_fields`, `sortable_fields`, `get_by_code` (including deleted rows), `list_page_search(params, *, search, is_active)`.
   - `is_used`: for Brand, an EXISTS over live `items`; for Manufacturer, over live `items`, live `item_vendor_links` and live `brands`.
   - `build_search_statement`: add `Item.brand_id == filters.brand_id`.
4. **`products/service.py`**
   - Audit constants and audited fields.
   - `create_brand`, `update_brand` (retire refused while in use, using `code=ErrorCode.REFERENCED_RECORD_IN_USE` and ideally a `_lifecycle`-style StateMachine with `record_status_change`), `list_brands`; the same for manufacturers.
   - `_require_brand` and `_require_manufacturer`, copying `_require_attribute` without the kind check.
   - Call them in `create_item` and `update_item`, and add the missing `_require_attributes` to `create_item`.
   - Extend `ITEM_AUDITED_FIELDS`.
   - Validate `manufacturer_id` in `link_vendor` and `update_vendor_link`.
5. **`products/router.py`**: `GET/POST /products/brands`, `PATCH /products/brands/{brand_id}`, and the same for `/products/manufacturers`, using `require_permission(Permission.BRAND_VIEW)` and the other literal permissions.
6. **`src/core/permissions.py`**
   - `BRAND_VIEW="brand.view"`, `BRAND_MANAGE="brand.manage"`, `MANUFACTURER_VIEW`, `MANUFACTURER_MANAGE`.
   - Add `"brand","manufacturer"` to `PERMISSION_GROUPS["Item Master"]`.
   - Presets: DESIGN_ENGINEER gets view and manage; `_SALES` gets `brand.view`; `_PURCHASE` gets `manufacturer.view`.
   - A cheaper alternative is to reuse `item_attribute.*`; if so, record it as an ASSUMPTION.
7. **Migrations.**
   - (a) Expand: create both tables with actor FKs, add the item and link columns with `NOT VALID` FKs, then `VALIDATE`.
   - (b) Grant migration, copied from `2f5e08c7ba61`.
8. **Frontend.**
   - `lib/permissions.ts`.
   - `masters-model.ts`: `mastersKeys.brands` and `.manufacturers`; `BrandRow = SchemaOf<'BrandOut'>`.
   - `brand-dialogs.tsx` and `manufacturer-dialogs.tsx`, copied from `attribute-dialogs.tsx`. Invalidate `mastersKeys.all` and the options keys.
   - `masters-page.tsx`: add `TabKey` entries, the tabs, `BrandsTab` and `ManufacturersTab` (copy `AttributesTab`), and update the empty-state text.
   - `router.tsx` and `navigation.ts` `/masters`: add `BRAND_VIEW`, `MANUFACTURER_VIEW` and the missing `ITEM_ATTRIBUTE_VIEW`.
   - `hooks/use-brand-options.ts`.
   - `item-model.ts` `ItemDetail`: add `brand_id`, `manufacturer_id`, `manufacturer_part_number` after the backend lands.
   - `item-form-dialog.tsx`: add two Selects and an MPN input, with hooks gated on `can(P.BRAND_VIEW)` and `can(P.MANUFACTURER_VIEW)`.
   - `item-detail-page.tsx`: labels.
   - `items-page.tsx`: a brand filter in the URL.
   - `VendorsPanel`: an MPN column.
9. **Tests.**
   - Integration tests copied from 1040-1305, plus: a cross-tenant brand id refused on **create** and update, retire refused while in use, and a manufacturer in use by a vendor link.
   - Frontend dialog and tab tests.
   - Run `make contract`.
10. **Docs.** `database.md` §2 and §3 (the "nothing branches on a brand" rule), a new `assumptions.md` section, `api.md`, and the handbook.

### B. Cost Centre master (organisation module) and its document references

1. **Master** (`organization/models.py`): `class CostCentre(TenantMasterEntity)` with `"cost_centres"`:
   - `code String(32)`, `name String(255)`, `parent_id` (self-FK, RESTRICT, indexed; a tree like Tally cost categories)
   - `cost_centre_type String(24)`: a reporting-only StrEnum of PRODUCTION, SERVICE, ADMINISTRATION, SELLING with a CHECK via `sql_in_list`. Nothing may branch on it, which is the same rule as `item_group`.
   - `plant_id` (FK plants, RESTRICT, nullable), `head_user_id` (FK users, SET NULL), `description Text|None`, `is_active`
   - `uq_cost_centres_company_id_code`, `ix_cost_centres_company_active`, `ck_cost_centres_parent_not_self`
   - Typical seed examples for an architectural-hardware plant, to be confirmed with Atcraft and not invented as rules: die-casting, machining (CNC/VMC), press shop, buffing and polishing, plating/PVD/powder coat (often job-worked), assembly, packing, QC, tool room, maintenance and utilities, stores, administration, and sales by zone.
2. **Schemas, repository, service, router.**
   - Copy Department exactly: `CostCentreCreate`, `CostCentreUpdate`, `CostCentreRead`, `CostCentreFilters`.
   - `CostCentreRepository(_OrgRepositoryMixin, BaseRepository[CostCentre])` with `ancestor_ids`, `has_live_children`, and raw-SQL `to_regclass` reference probes for every carrier table.
   - `COST_CENTRE_LIFECYCLE`; `CostCentreService.create_cost_centre`, `update_cost_centre`, `delete_cost_centre(id, *, reason)`.
   - A public `require_cost_centre(session, tenant, cost_centre_id)` (tenant plus active) and a batch `existing_cost_centre_ids(ids)` for postings.
   - Routes `/organization/cost-centres` gated on `Permission.COST_CENTRE_VIEW` and `COST_CENTRE_MANAGE` (`"cost_centre.view"`, `"cost_centre.manage"`; group "Organisation"; presets: `_ACCOUNTS` view and manage, `_PURCHASE`, `_PRODUCTION` and PLANNER view). Add the grant migration.
3. **Frontend.**
   - `admin-types.ts` gets `orgKeys.costCentres`; derive the row type with `SchemaOf<'CostCentreRead'>`.
   - `use-organisation-options.ts` gets `useCostCentreOptions` with key `['organization','cost-centres','options']`.
   - `organisation-page.tsx` gets a `'cost-centres'` tab gated on `P.COST_CENTRE_VIEW`, a `CostCentresTab` copied from `DepartmentsTab`, and reuses `RetireDialog`.
   - `organisation-dialogs.tsx` gets `CostCentreFormDialog`, with parent options from a full reference list.
   - Add `P.COST_CENTRE_VIEW` to the `/admin/organisation` route rights and write the missing page tests.
4. **Document references, in order of value.** Each is a nullable FK with RESTRICT, validated in tenant, and records an ASSUMPTION about default precedence: explicit value, then source document, then department default, then NULL meaning unallocated.
   1. `departments.cost_centre_id` (default for users) and `work_centers.cost_centre_id` (production cost pools).
   2. `purchase_requisitions` (header and lines), then `purchase_orders` and `purchase_order_lines` (copied through `requisition_line_id`, including the MRP and quotation conversions), then `vendor_invoices` and `vendor_invoice_lines` (defaulted from the PO line; direct bills such as power, freight and job-work charges).
   3. `ledger_entries.cost_centre_id`. This is the analytic source of truth. Set it through `JournalLine.cost_centre_id` in `post_journal`, allow it only on INCOME and EXPENSE accounts, copy it in `reverse_journal`, and split the invoice's `PURCHASE_EXPENSE` leg per line cost centre. Index `(company_id, cost_centre_id, entry_date) WHERE cost_centre_id IS NOT NULL`.
   4. `job_work_orders`, `production_orders` (material issues inherit), and `stock_adjustments` (write-offs).
   5. A cost-centre expense report from `ledger_entries`.
   - Do not touch `inventory_transactions`.
5. **ASSUMPTIONs to record in `docs/assumptions.md`:**
   - Cost centres are optional; NULL means unallocated.
   - Only INCOME and EXPENSE ledger legs may carry one.
   - Retirement is refused while a live department or work-centre default, a child, or an open document references the cost centre; posted history keeps it, and reversals copy it even after retirement.
   - The tree depth limit, as a named constant.
   - The defaulting precedence above.
6. **Prerequisite fix.** In the same change, add tenant validation for the existing `purchase_requisitions.department_id` and `branch_id` and `purchase_orders.branch_id` (trap 3), and extend the department and plant retire guards (trap 2). Otherwise the new column sits beside two unchecked ones.
