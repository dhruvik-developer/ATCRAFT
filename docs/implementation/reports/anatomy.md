# Cross-cutting feature anatomy: how to add an entity, document or module and its screen (ATCRAFT ERP)

Scope: this is a read-only map of the shared plumbing every new feature goes through. All paths are relative to `C:\Project\Other\ATCRAFT`. Nothing was modified.

---

## Exists today

### A. Backend core

#### A1. `apps/api/src/core/models.py`: base classes and mixins

- **`NAMING_CONVENTION` (35–41)**
  - `ix_%(table_name)s_%(column_0_N_name)s`, `uq_…`
  - `ck_%(table_name)s_%(constraint_name)s`
  - `fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s`
  - `pk_%(table_name)s`
  - So `CheckConstraint(..., name="status_known")` on `sales_invoices` is emitted as `ck_sales_invoices_status_known`.
- **`Base(DeclarativeBase)` (44)**: `type_annotation_map = {uuid.UUID: GUID}`.
- **Mixins**
  - `UUIDPrimaryKeyMixin` (58): `id`, default `new_id` (UUIDv7).
  - `TimestampMixin` (69): `created_at` uses `server_default=func.now()` and is indexed. `updated_at` has a server default but uses **Python-side** `default=utcnow, onupdate=utcnow` (see the MissingGreenlet trap).
  - `SoftDeleteMixin` (98): `deleted_at`.
  - `TenantMixin` (120): `company_id` FK to `companies` with `ondelete RESTRICT`, indexed.
  - `ActorMixin` (140): `created_by_id` / `updated_by_id`, FK to `users` with `SET NULL`.
  - `OptimisticLockMixin` (168–184): an int `version` column plus `__mapper_args__ = {"version_id_col": version}`. A `StaleDataError` becomes a 409 `CONFLICT` via `stale_data_handler` (`core/exception_handlers.py:223`).
- **Composites**
  - `TenantEntity` (190) = UUID PK + tenant + timestamps + actors.
  - `TenantMasterEntity` (196) = `TenantEntity` + soft delete. Use for master data.
  - `ImmutableLedgerEntity` (202) = only `id`, `company_id`, `created_at`, `created_by_id`.
- **CHECK helpers**: `sql_in_list(enum)` (220) and `sql_array_literal(enum)` (234). 69 CHECK/partial-index uses across the models.
- **Who uses `OptimisticLockMixin`**: `SalesOrder` (`sales/models.py:135`), `PurchaseOrder` (`procurement/models.py:792`), `VendorInvoice` (`finance/models.py:487`), `Payment` (760), `Dispatch`, `PackingOrder`, `JobWorkOrder`, `ProductionOrder`, `AssemblyOrder`.
  - BOM and Routing cannot use it, because `version` is already a business column there. They hand-roll `row_version` (`bom/models.py:179-187`).

#### A2. `core/deps.py`: identity, tenant scope and authorisation

- **Type aliases**: `DbSession` (51), `CurrentUser = Annotated[Principal, Depends(get_current_principal)]` (277), `Tenant = Annotated[TenantContext, Depends(get_tenant_context)]` (360).
- **`Principal` (67–141)**, a frozen dataclass.
  - Fields: `user_id, company_id, email, full_name, is_superuser, permissions: frozenset[str], must_change_password, session_id, role_codes, vendor_id, allowed_category_ids, allowed_warehouse_ids, branch_id, plant_id, department_id`.
  - `has` / `has_any` / `has_all` / `require` all short-circuit to True when `is_superuser`.
  - `is_vendor_user` means `vendor_id is not None`.
  - `can_access_category` / `can_access_warehouse`: an empty restriction tuple means unrestricted.
  - `assert_same_tenant` raises `TenantMismatchError`.
- **Permission resolution**
  - `_resolve_permissions` (148): union of the permissions of active, non-deleted roles, plus `users.extra_permissions`, minus `users.denied_permissions` (denials are applied last).
  - Loaded fresh from the database every request by `_load_principal` (171). There is no permission cache; only session liveness is cached, for 30 s.
  - `get_current_principal` (249) also confines temporary-password principals to `password_change_route_allowed` (54).
- **Route guards**
  - `require_permission(*permissions, mode="all")` (280) **returns a `Depends`**. Usage is `dependencies=[require_permission(Permission.X)]`.
  - `require_any_permission` (309), `require_superuser` (313).
  - `forbid_vendor_users` (319) raises `AuthorizationError(code=SCOPE_DENIED)`.
- **`TenantContext` (333)**: `company_id, user_id, vendor_id, allowed_category_ids, allowed_warehouse_ids, is_superuser`. `get_tenant_context` (344) is the only source of `company_id`. `permissions_of` is at 363.
- **How vendor-portal users are represented**
  - Columns on `users`: `vendor_id`, `allowed_category_ids`, `allowed_warehouse_ids` (`users/models.py:150-160`).
  - `roles.is_vendor_role` (79).
  - `assert_vendor_role_consistency` (`users/service.py:466`) enforces both directions: vendor roles only on vendor-bound accounts, and only vendor roles on those accounts.
  - `MeResponse.vendor_id` (`auth/schemas.py:174`) feeds the frontend `useAuth().isVendorUser` (`features/auth/auth-context.tsx:351`).
  - It is the **same SPA**. There is no separate vendor app.
- **How vendor-portal scope is checked**
  - Scoping is per repository: `if self.tenant.vendor_id is not None: stmt = stmt.where(Model.vendor_id == self.tenant.vendor_id)`.
  - It exists only in `procurement/repository.py` (675, 908, 1216, 1320), `vendors/repository.py` (130, 365), `users/repository.py` (119) and `products/repository.py` (534, category filter).
  - `forbid_vendor_users` is used only on 2 procurement routes.

#### A3. `core/permissions.py`: the permission catalogue

- **`Permission(StrEnum)` (16–205)**: strings of the form `resource.action`.
  - Already present and relevant to the gaps: `DEALER_VIEW/MANAGE` (46–47), `SALES_INVOICE_VIEW/CREATE` (116–117), `PURCHASE_RETURN_VIEW/CREATE` (142–143), `VENDOR_INVOICE_*` (188–190), `JOB_WORK_*` (165–169), `REPORT_VIEW/EXPORT`, `NOTIFICATION_VIEW`, `SETTINGS_*`, `DEPARTMENT_*`.
- **Grouping**
  - `PERMISSION_GROUPS` (210–242) maps a display group to resource prefixes. `"Sales": ("sales_order", "sales_invoice")`.
  - `group_for` (245) returns `"Other"` when a prefix is unlisted.
- **Preset building blocks**
  - `VIEW_ONLY` (257): every permission ending `.view`, `.ledger_view`, `.cost_view` or `.valuation_view`.
  - Bundles: `_STORE` 263, `_PURCHASE` 291, `_PRODUCTION` 326, `_QC` 362, `_DISPATCH` 380, `_SALES` 400 (has `SALES_INVOICE_VIEW`), `_ACCOUNTS` 422 (has `SALES_INVOICE_VIEW` and `SALES_INVOICE_CREATE`), `_VENDOR_BASE` 451.
- **`ROLE_PRESETS` (460–677)**
  - **System roles**:
    - `SUPER_ADMIN` = `frozenset(Permission)`.
    - `ADMIN` = everything except `COMPANY_MANAGE`.
    - The vendor roles `VENDOR_OWNER`, `VENDOR_MANAGER`, `VENDOR_DISPATCH_MANAGER`, `VENDOR_QC_CONTACT`, `VENDOR_ACCOUNT_MANAGER`, all with `is_vendor_role`.
  - **Non-system roles (client-owned)**: `PLANT_MANAGER`, `STORE_KEEPER`, `PURCHASE_MANAGER`, `PURCHASE_EXECUTIVE`, `PRODUCTION_SUPERVISOR`, `QC_INSPECTOR`, `DISPATCH_MANAGER`, `SALES_EXECUTIVE`, `ACCOUNTS`, `PLANNER`, `DESIGN_ENGINEER`, and `AUDITOR` (`VIEW_ONLY` plus `AUDIT_LOG_VIEW` and `REPORT_EXPORT`).
  - `VENDOR_ROLE_CODES` (679), `ALL_PERMISSIONS` (683).
- **Catalogue endpoint**: `users.service.permission_catalogue()` (`users/service.py:579`), served at `GET /roles/permissions/catalogue` (`users/router.py:354`, gated on `ROLE_VIEW`).
- **Approval queue**: `audit/query_service.py:186` derives `DECIDABLE_PERMISSIONS` from every permission ending `.approve` or `_approve`. A new `x.approve` therefore joins the approval queue automatically, and so does the frontend dashboard panel, which uses the same suffix rule.

#### A4. `core/seed.py`: baseline data

- **Command**: `python -m src.core.seed [--company --company-code --admin-email --admin-password]`. `DEFAULT_ADMIN_EMAIL = "admin@atcraft.example"` (47).
- **`_seed_roles` (193–248)**: creates any missing preset role. For an **existing** role it rewrites `permissions` only when `role.is_system` (215–231).
- **`_seed_admin` (251)**: the admin is `is_superuser=True`, holds `SUPER_ADMIN`, and must change the password.
- **`_seed_branch`** creates `HO`.
- **`_seed_optional` (313–465)**, idempotent by code:
  - `UNITS` (52) and `UOM_CONVERSIONS` (124).
  - `ITEM_CATEGORIES` (150).
  - `WAREHOUSES` (137–148), including **`WH-RET` "Sales Return Store", type `RETURN` (147)**, plus `WH-QC`, `WH-JW` and `WH-SCR`.
- **A release does not run the seeder.** `scripts/deploy.sh` runs `alembic upgrade head` (532, 649, 752); `seed` is a separate manual verb (678–681).

#### A5. How a new permission reaches existing tenants (precedent `20260906_0902_grant_item_attribute_permissions.py`)

1. `roles.permissions` is a stored text array, so adding the enum member alone reaches no deployed role.
2. A seeder re-run refreshes only `is_system` roles. Deploy does not re-run it.
3. So ship a **data migration** that appends the literal strings to the system roles:
   ```python
   UPDATE roles SET permissions = array_append(permissions, :permission)
   WHERE is_system AND code = ANY(:codes) AND NOT (:permission = ANY(permissions))
   ```
   - Strings are spelled literally; never import `ROLE_PRESETS`.
   - `SYSTEM_ROLES = ("SUPER_ADMIN", "ADMIN")` in that precedent. Add vendor role codes if a vendor role must get the right.
   - `downgrade()` uses `array_remove`.
4. Non-system roles are client-owned. Existing tenants do **not** get the new permission; an admin grants it in Settings → Roles. New companies get it from the preset.
5. `is_superuser` users see everything anyway, because `Principal.has` short-circuits.

#### A6. `core/state_machine.py`

- **`Transition(source, target, permission=None, label="", is_irreversible=False)` (26–40).**
- **`StateMachine(entity, initial, transitions, terminal)` (43–121)**:
  - `allowed_targets`, `find`, `can_transition`.
  - `assert_transition` (64) raises `InvalidStateTransitionError` (422, details `current_status`, `requested_status`, `allowed`).
  - `required_permission`, `is_terminal`, `to_graph`.
  - `available_actions(current, held)` (83), which **no route uses**.
  - `validate()` (103): fails on unreachable states or non-terminal dead ends.
- **`Transition.permission` is metadata.** `assert_transition` does not check it. Enforcement is the route's `require_permission`; only `job_work/service.py:1961` calls `principal.require` inside a service.
- **Canonical document helper**: procurement `_transition` (`procurement/service.py:432`): `current = status_enum(entity.status)` → `machine.assert_transition` → set status and `updated_by_id` → `flush` → `record_status_change(...)`. Sales has its own `_transition` (`sales/service.py:1825`).
- **Master lifecycle factory**: `organization/service.py:200` `_lifecycle(entity, manage)` gives ACTIVE ↔ INACTIVE, with ARCHIVED terminal. It backs `BRANCH_LIFECYCLE`, `PLANT_LIFECYCLE`, `DEPARTMENT_LIFECYCLE` and `FINANCIAL_YEAR_LIFECYCLE` (242–245).

#### A7. Audit API (`src/modules/audit/service.py`)

```python
async def record_audit(session, *, company_id, action: AuditAction, entity_type: str,
    entity_id=None, entity_code=None, summary="", user_id=None, actor_label=None,
    old_values=None, new_values=None, context=None, flush=True) -> AuditLog      # :104
async def record_status_change(session, *, company_id, entity_type, entity_id, entity_code,
    from_status: str, to_status: str, user_id, action=AuditAction.STATUS_CHANGE,
    reason=None, flush=True) -> AuditLog                                          # :158
def snapshot(instance, fields: list[str]) -> dict                                # :193
```

- It stores diffs only (`diff_values`, :76). `_NEVER_LOG` (28) redacts secrets; Decimals are serialised as strings.
- `AuditAction` (`audit/models.py:24-64`) includes CREATE, UPDATE, DELETE, APPROVE, REJECT, CANCEL, STATUS_CHANGE, DATA_EXPORT, INVENTORY_MOVEMENT, PAYMENT, JOB_STARTED and others.
- `AuditLog` (67) is append-only, enforced by the DB trigger `trg_audit_logs_immutable`. `ApprovalRequest` is at 113.
- Convention: each module defines a snake_case `AUDIT_<X> = "<entity>"` constant (for example `AUDIT_PURCHASE_RETURN`) and an `<X>_AUDITED_FIELDS` list (for example `products/service.py:117-121`).

#### A8. `src/common/codes.py`: document numbering

- **`DocumentSequence`** (`document_sequences`, 32–50): unique on `(company_id, doc_type, period_key)`. Rows are created lazily, so a new doc type needs no migration.
- **`DocType(key, prefix, yearly=True, padding=5)`** (53).
- **`DocTypes` (65–91)**: SO, **INV (`sales_invoice`, already declared, unused)**, PR, RFQ, PO, GRN, PRTN, MO, WO, AO, JW, JWC, MI, MR, ST\*, ADJ\*, PC\*, QC, PK, DSP, VINV, PAY, CADJ, MRP\*, BOM\*. (\* = `yearly=False`.)
- **Format**: `financial_year_key(on, start_month=4)` gives `"2026-27"` (94).
  ```python
  async def next_document_number(session, company_id, doc_type, *, on=None, fy_start_month=4) -> str  # :101
  ```
  It returns `PREFIX/2026-27/00042`, or `PREFIX/00042` when not yearly. It uses `INSERT … ON CONFLICT DO UPDATE … RETURNING` inside the caller's transaction, so numbers are gapless.
- **Registering a new type** = one constant in `DocTypes`.
- **Code helpers**: `normalise_code` (150), `CODE_PATTERN` (168), `validate_code` (176), `clean_name` (190), `strip_optional` (198), `clean_text` (210).

#### A9. Errors, responses and pagination

- **`core/errors.py`**
  - `ErrorCode` (17–76).
  - `AppError` (79): status, code, message, details; renders `{"success": false, "error": {...}, "request_id"}`.
  - Classes: `ValidationError` 422, `NotFoundError(entity, id)` 404, `ConflictError` 409, `DuplicateError` 409 `DUPLICATE_DOCUMENT`, `AuthorizationError` 403, `PermissionDeniedError(permission)` 403, `BusinessRuleError` 422, `InvalidStateTransitionError`, `ApprovalRequiredError`, `InsufficientStockError`, `StockLockTimeoutError` 409.
  - `exception_handlers.py:138-151` maps database errors: unique violation → 409 `DUPLICATE_DOCUMENT`; FK on insert → 422; FK on delete → 409 `REFERENCED_RECORD_IN_USE`; CHECK → 422 `BUSINESS_RULE_VIOLATION`; NOT NULL → 422.
- **`core/responses.py`**
  - `ApiResponse[T].ok(data, message)` (26).
  - `OffsetPagination` (40, `total` may be null), `CursorPagination` (56).
  - `PaginatedResponse[T]` (64), `CursorPaginatedResponse[T]` (73).
  - `AcceptedJobResponse.create(job_id, status_url, message)` (96).
  - `api_path()` (116), `COMMON_ERROR_RESPONSES` (129).
- **`core/pagination.py`**
  - `MAX_PAGE_SIZE=200`, `DEFAULT_PAGE_SIZE=50`, `COUNT_CEILING=10_000` (above the ceiling `total=null`).
  - `PageParams` / `page_params` (68, 108).
  - `SortParams.resolve(allowed, default)` (96): an unknown `sort_by` gives 422 with `allowed_sort_fields`.
  - `bind_query_filters(model, **values)` (130): cross-field filter errors become 422, never 500.
  - `_stable_order` (228) appends the PK tiebreaker. `paginate` (308), `paginated` (363).
  - `Cursor`, `CursorParams`, `cursor_params`, `paginate_keyset(..., created_at_col, id_col)` (414) for keyset paging, descending on `(created_at, id)`.

#### A10. `common/repository.py`: `BaseRepository[ModelT]` (37–219)

- Class attributes: `model`, `sortable_fields: dict[str, InstrumentedAttribute]` (43, the whitelist), `searchable_fields` (45), `entity_label` (51).
- `_scoped(include_deleted=False)` (68) applies `company_id` and the `deleted_at` filter. `_apply_search` (75) does ILIKE across `searchable_fields`.
- Reads:
  - `get` and `get_or_404` (95–105). Not-found and cross-tenant give the same 404.
  - `get_many` (107), `get_by_field`, `exists`.
  - `list_page(params, *, stmt=None, order_by=None)` (131) takes **one** order clause.
  - `list_keyset` (147).
- Writes:
  - `add` (165) stamps `company_id` and `created_by_id` from the tenant.
  - `touch` (174) sets `updated_by_id`.
  - `soft_delete` (179), `hard_delete`, `bulk_update`, `bulk_delete`.
- Row locks are hand-written per repository, for example `PurchaseReturnRepository.lock_for_update` uses `.with_for_update()` (`procurement/repository.py`).

#### A11. `core/database.py`

- `get_db` (103): request-scoped session that rolls back on exception and never auto-commits.
- `session_scope()` (119) for workers, seed and scripts.
- **`transaction(session)` (140–194)**: the body always runs inside a SAVEPOINT. Nesting is tracked by a session flag (`_atcraft_business_transaction`), not by `in_transaction()`. Only the outermost frame commits.
- `advisory_xact_lock` (206), `try_advisory_xact_lock` (224), `set_lock_timeout` (234).

#### A12. Small core modules

- **`core/registry.py`**: `import_all_models()` (29) imports every `src/modules/*/models.py` plus `src.common.codes`. `get_metadata()` (62) is used by Alembic `env.py` and by test schema creation, so new tables are discovered automatically.
- **`core/constants.py`**: only `DEFAULT_CURRENCY="INR"` (18) and `CURRENCY_CODE_LENGTH=3` (21).
- **`core/messages.py`**: `FIELDS_NEED_ATTENTION`, `NO_FIELDS_TO_UPDATE`, three auth sentences, `inactive_record(entity, code, consequence)`, `date_before(later, earlier)`. Rule: only sentences raised verbatim from two or more modules belong here.
- **`api/router.py`**
  - `MODULE_ORDER` (23–49) lists all 25 modules.
  - `register_module_routers()` (52) imports `src.modules.<name>.router` and requires an attribute named `router`.
  - Mounting happens in the app lifespan (`main.py:151`) under `settings.api_v1_prefix`.
  - **A module missing from `MODULE_ORDER` is never mounted**, even though its models still register.

#### A13. Background work, notifications and PDFs

- **`workers/celery_app.py`**
  - `include=[mrp, reports, notifications, maintenance]` (38).
  - `task_routes` (65) route by task-name prefix: `mrp.*`, `reports.*`, `notifications.*`, `maintenance.*`.
  - `beat_schedule` (71): inventory summary refresh, stale reservation release, **`notifications.scan_dispatch_delays` hourly** (the only alert sweep), session purge, temp-file purge, password-reset outbox.
  - Tasks are registered with `@async_task(name="area.verb", queue="...")` (137).
- **Notifications**
  - `notify(session, *, company_id, user_ids, notification_type, title, body, severity, entity_type, entity_id, action_url, group_key, actor_id)` (`notifications/service.py:364`).
  - `notify_role(..., role_codes=...)` (519).
  - `NotificationChannel` has only IN_APP and EMAIL (`notifications/models.py:82`).
  - `NotificationType` (93) declares 20 types, but **only `QC_FAILED`, `REPORT_READY`, `DISPATCH_DELAYED` and `ALLOCATION_CHANGED` are ever raised** anywhere in `src`.
- **PDF pipeline (precedent)**
  - The route returns `AcceptedJobResponse` (`packing/router.py:474-494`).
  - `packing.service.queue_packing_list` (2029) creates a `report_snapshots` job via `reports_service.new_job_snapshot(ReportKey.PACKING_LIST, ...)` (`reports/service.py:1027`), audits `JOB_STARTED`, then calls `dispatch_job` (1048).
  - The worker `reports.generate_packing_list` (`workers/tasks/reports.py:328`) runs `start_snapshot` → `packing_list_data` → `render_packing_list_pdf(data) -> bytes` (248, reportlab, run in a thread) → writes a `Document` row with `category=REPORT`, `entity_type="packing_order"` → audits `DATA_EXPORT` → `complete_snapshot`. On failure it unlinks the file and calls `fail_snapshot`.
  - `reportlab` and `openpyxl` are already dependencies (`pyproject.toml:52-53`).

### B. Migrations

- **Conventions**
  - `alembic.ini` uses `file_template = YYYYMMDD_HHMM_<slug>`, timezone UTC, and a `ruff format` post-write hook.
  - `script.py.mako` carries the docstring header (`Revision ID`, `Revises`, `Created`) and the **Review checklist**: Locking impact, Data migration, Rollback, Production size, Index creation.
  - Revision ids are 12 hex characters.
  - `env.py`: URL from `settings.database_sync_url` (psycopg); `compare_type` and `compare_server_default`; `include_object` skips `pg_stat_statements` and `pg_*` indexes; empty autogenerate is suppressed; `transaction_per_migration=True`; **no `render_item` hook**.
- **Chain** (15 migrations; the 16th file is `.gitkeep`). Filename order is not chain order: rows 4–6 below.

| # | file | revision | down_revision |
|---|---|---|---|
| 1 | 20260831_2130_initial_schema | b7cc02046b2d | — |
| 2 | 20260902_0000_mrp_requirement_late_supply | ad9f573e5e59 | b7cc02046b2d |
| 3 | 20260902_0642_fifo_cost_layers | 65bcadcc236b | ad9f573e5e59 |
| 4 | 20260902_1210_mrp_per_feed_late_supply_dates | 66b1c0e2d1f4 | 65bcadcc236b |
| 5 | 20260902_1420_fifo_opening_layer_correction | 7a1e4c9b2d55 | 66b1c0e2d1f4 |
| 6 | 20260902_1054_mrp_late_supply_closing_dates | 2438d960a6e4 | 7a1e4c9b2d55 |
| 7 | 20260902_1147_drop_items_valuation_method | 7e8eb4bdd0ed | 2438d960a6e4 |
| 8 | 20260904_0000_secure_totp_mfa | a4f7c29d8e11 | 7e8eb4bdd0ed |
| 9 | 20260904_0100_email_delivery_state | d91ec6b47a20 | a4f7c29d8e11 |
| 10 | 20260905_0000_purchase_return_line_stock_state | 3c8f2a7d5b91 | d91ec6b47a20 |
| 11 | 20260905_0100_report_snapshots_packing_list_key | 5e2b8c41f7a3 | 3c8f2a7d5b91 |
| 12 | 20260905_0200_ensure_extensions | 7f3a91c4e5d2 | 5e2b8c41f7a3 |
| 13 | 20260906_0900_item_attributes_master | 4c1d70b8ea92 | 7f3a91c4e5d2 |
| 14 | 20260906_0901_drop_item_attribute_text | 9b6f2ac41d38 | 4c1d70b8ea92 |
| 15 | 20260906_0902_grant_item_attribute_permissions | **2f5e08c7ba61 (HEAD)** | 9b6f2ac41d38 |

- **Data-migration precedents**
  - **0900 (expand)**: `op.create_table` with explicit `op.f()` names; `sa.Uuid()` rather than GUID; `ADD COLUMN NULL`; FKs `NOT VALID` then `VALIDATE CONSTRAINT`; set-based backfill via `op.get_bind().execute(sa.text(...))`; logging to `alembic.runtime.migration`; refuses with `RuntimeError` on orphans; lossless downgrade.
  - **0901 (contract)**: pre-flight query that names collisions; swaps an expression unique index; partial downgrade, with the loss documented honestly.
  - **0902**: literal-string permission grant, shown in A5.
  - **5e2b8c41f7a3**: widens a `report_key IN (...)` CHECK. It drops the constraint, recreates it `NOT VALID` with **literal** key lists, then validates.
- **Triggers**: Alembic does not model them. The initial schema defines `atcraft_reject_row_mutation()` and `IMMUTABILITY_TRIGGERS` for `audit_logs`, `inventory_transactions` and `login_attempts` (`initial_schema.py:197-217`). `ledger_entries` has none.
- **Guards on migrations**
  - **Single head**: no test. It is enforced implicitly by `alembic upgrade head` in CI (`ci.yml:87`, from an empty database) and in deploy.
  - **Model ↔ migration drift**: **no `alembic check` anywhere.** `test_mrp_late_supply_migration.py:27-31` explains why it is avoided.
  - **Identifier length**: `tests/unit/test_schema_identifier_length.py`, metadata-only, seconds.
  - **Round trip**: three per-revision integration tests (`tests/integration/test_mrp_*_migration.py`). Each runs alembic in a subprocess against a throwaway database, with `ALEMBIC_TIMEOUT_SECONDS=600` and `stdin=DEVNULL`. Cost: the initial-schema upgrade takes about 10 s on CI and took 2 min 17 s on the loaded 4-core laptop (per the project's local notes).

### C. Tests

- **`tests/conftest.py`**
  - Forces `APP_ENV=test`, `CACHE_ENABLED=false`, `RATE_LIMIT_ENABLED=false`. Reads `POSTGRES_*` from the root `.env` (host `127.0.0.1`). The test database is `atcraft_erp_test`.
  - `_database` (session scope): `prepare_metadata()` → `drop_all` / `create_all` from **metadata, not migrations**.
  - Fixtures: `engine`, `session` (outer transaction rolled back; `join_transaction_mode="create_savepoint"`), `committed_session`, `company`, `other_company`, `admin_role` (all permissions, non-system), `user`, `tenant` (`TenantContext`), `principal` (all permissions), `limited_principal` (`.view` only), `api_client` (httpx `ASGITransport`; overrides `get_db` and `get_current_principal`; `base_url=http://test/api/v1`), `unique_code`, `decimal_equal`.
- **`tests/api/conftest.py`**: autouse `_require_database` and `_mount_module_routers`. Without the latter every `tests/api` request 404s. **`tests/integration/conftest.py`**: `_require_database`.
- **`tests/schema_support.py`**: `prepare_metadata()` (117) imports all models and registers one-column stand-ins for dangling FK targets. `report_database_setup_failure()` (66): skip locally, fail under `CI`, re-raise real defects.
- **`tests/seed_support.py`** (ORM builders that flush and take `**overrides`): `seed_company`, `seed_user`, `seed_uom`, `seed_item(session, *, company_id, code, name=None, base_uom_id=None, item_group=RAW_MATERIAL, **overrides)`, `seed_warehouse`, `seed_category`, `seed_customer(..., blocked=False)`, `seed_vendor`, `seed_serial`.
- **Module-scoped schema pattern**: `tests/integration/test_products_service.py`. Each module gets a private PostgreSQL schema built once with `create_all(checkfirst=False)`.
- **pytest config** (`pyproject.toml`): `asyncio_mode=auto`, `--strict-markers --strict-config`, markers `unit | integration | api | concurrency`, `filterwarnings = error::DeprecationWarning:src.*`. mypy runs with `disallow_untyped_defs` on `src`.
- **Suite sizes and timings** (project notes, 2026-09-05): about 2,833 unit tests, 1,201 integration/API tests, 735 vitest tests. vitest takes about 14 minutes with `--no-file-parallelism --maxWorkers=1`. The integration suite takes over an hour on the local box. e2e is 12 tests and takes about 25 s.

**Meta and guard tests that police new code**

| Test | What it enforces | What a new feature must do |
|---|---|---|
| `unit/test_frontend_permission_catalogue.py` | `Permission` enum and `apps/web/src/lib/permissions.ts` match exactly, in both directions. Parses `'([a-z_]+\.[a-z_]+)'`. | Add `P.X: 'x.y'` for every new permission. Keep strings to letters and underscores; digits are not parsed. |
| `unit/test_frontend_permission_gates.py` | Every non-test `api.post/put/patch/delete(path)` must name `P.<perm>` (anywhere in the same feature folder or in `router.tsx`), where `<perm>` is the **first `Permission.X` literal in that backend route's decorator**. Parses `@<var>.<method>("literal"…)`, `APIRouter(prefix="…")` and `include_router(x, prefix=…)`. `EXEMPT` is empty. | Put `require_permission(Permission.X)` literally in the decorator. Aliases such as `FY_VIEW` are invisible to the parser. Use literal paths and prefixes. Gate the UI control on that exact right. |
| `unit/test_frontend_contract_types.py` | Hand-written TS interfaces registered in `MIRRORS` may not declare fields the Pydantic model lacks. Every `export interface` in `features/items/item-model.ts` must be registered. | Derive types with `SchemaOf<'X'>`. Register any hand-written mirror, for example for an endpoint with `response_model=None`. |
| `unit/test_frontend_constants.py` | No literal `300` in a `setTimeout`, and no `limit: 20` or `limit: 200`, anywhere in `apps/web/src`, **tests included**. | Import `SEARCH_DEBOUNCE_MS`, `PICKER_PAGE_SIZE`, `LOOKUP_LIST_LIMIT`. |
| `unit/test_frontend_field_errors.py` | Exactly one `applyFieldErrors` (`lib/form-errors.ts`). | Reuse it. |
| `unit/test_role_presets_reach_roles.py` | `SUPER_ADMIN` ⊇ all permissions. Every resource prefix is in `PERMISSION_GROUPS`. The system-role set is exactly `[ADMIN, SUPER_ADMIN, VENDOR_*×5]`. | Add each new resource prefix to a group. Do not add `is_system` presets. |
| `unit/test_users_permissions.py` (345–348) | The catalogue's `"Other"` group stays empty. | Same as above. |
| `unit/test_schema_identifier_length.py` | Every table, column, constraint and index name is ≤ 63 bytes, and the emitted name equals the declared one. The ledger of 12 known long FKs may only shrink. | Give long FKs and CHECKs explicit short names. |
| `unit/test_state_machine_entity_names.py` | Every module-level `StateMachine` in `src.modules.<pkg>.service` (plus `audit.query_service` and `procurement.service_rules`) has an `entity` matching `^(?:[A-Z]+\|[A-Z][a-z]+)( [a-z0-9]+)*$`. At least 30 machines must be found. | Name it "Sales invoice", not "SalesInvoice". A machine defined in another file must be added to `_EXTRA_MODULES`, or it is silently unchecked. |
| `unit/test_repository_entity_label.py` | Every concrete `BaseRepository` in `<pkg>.repository` has a label with no lowercase-uppercase adjacency. | Set `entity_label` for acronyms. |
| `unit/test_entity_names.py` | Shared `X_ENTITY` constants: repository label = service = machine. | Optional pattern to follow. |
| `unit/test_status_badge_catalogue.py` | Every member value of any `class *Status*/*State*(StrEnum)` under `apps/api/src` is a key of `STATUS_VARIANTS` in `components/shared.tsx`. | Add new status values, such as `ISSUED` (exists), `CREDITED`, `VOID`, `OVERDUE`. Must be written as `StrEnum`. |
| `unit/test_e2e_sweep_routes.py` | Every static route in `route-loaders.ts` appears in `browser-sweep` `STATIC_ROUTES`, `a11y-sweep` `ROUTES` and `responsive-sweep` `STATIC_ROUTES`. `permission-sweep` `ROUTES` names one of the route's permissions, or `null` if ungated. `theme-sweep` uses only real routes. | Add each new page to all four scripts. |
| `unit/test_postman_and_contract_targets.py` | `postman_collection.json` equals what the exporter renders from `openapi.json`. Every operation is present. `docs/api.md` contains `"<N> requests in <F> folders"` (currently 494 / 27). Makefile and make.ps1 targets. | Run `make contract`. Update the count in `docs/api.md:28`. |
| `unit/test_env_template.py` | `.env.example` matches `Settings` fields, Compose keys, conftest keys and VITE keys. | Add a line for any new setting, for example alert thresholds or SMS keys. |
| `unit/test_inventory_movement_rules.py:100`, `test_inventory_serial_lifecycle.py:96` | `TRANSACTION_SIGNS` and `SERIAL_MOVEMENT_TARGETS` cover all of `TransactionType`. | Only if adding a transaction type. |
| `unit/test_reports_catalogue.py` and the import-time assert in `reports/service.py:744-756` | Every `ReportKey` has exactly one definition unless it is document-only. | Applies when adding a report. |
| `unit/test_query_filter_binding.py`, `unit/test_inventory_query_binding.py` | Cross-field filter errors produce 422. | Build filter models with `bind_query_filters`. |
| Per-module `unit/test_<module>_lifecycle.py` (convention) | `MACHINE.validate()`, happy path, terminal states. | Write one per machine, as in `test_sales_lifecycle.py`. |
| `api/test_route_writes_persist.py` | `transaction()` really commits through the real `get_db` chain. | Nothing to do. |
| Web `app/route-loaders.test.ts` | Every `navigation` `to` has a loader. Keys are absolute. | Add a loader for each nav item. |
| Web `app/router.test.tsx`, `app-layout.test.tsx` | Gate and sidebar behaviour. | Nothing to do. |

**Rules with no machine guard at all**:

- every route carries `require_permission`
- list endpoints are paginated
- no float near money
- frontend sortable columns ⊆ backend `sortable_fields`
- frontend `NOTIFICATION_TYPES` and the `ReportKey` union match the backend enums
- an enum member added under a `sql_in_list` CHECK has a migration
- models match migrations

### D. Frontend anatomy

#### D1. Routing and navigation

- **`app/route-loaders.ts`**: `routeLoaders` (24–76) maps path → `() => import(page)`. The page module **must have a default export**. `prefetchRoute` is at 90.
- **`app/router.tsx`**:
  - `const XPage = lazy(routeLoaders['/x'])` (25–74).
  - `routeGroups: {path, element, permissions: PermissionKey[]}[]` (249–375).
  - `RequirePermission` (136) uses `canAny`; a denied route never imports its chunk.
- **`app/navigation.ts`**:
  - `NavItem {label, to, icon, permissions?, badgeKey?}` (46). Any one listed permission reveals the item.
  - Sections, in order: Overview, Sales, Inventory, Engineering, Planning, Procurement, Manufacturing, Quality & Dispatch, Finance, Insights, Administration.

#### D2. Permissions and server state

- **`lib/permissions.ts`**: the `P` const (14–202) already holds `SALES_INVOICE_VIEW/CREATE` (194–195) and `DEALER_*`. `PermissionKey` is at 204.
- **`useAuth()`** (`features/auth/auth-context.tsx:152-163, 334-351`) gives `can`, `canAny`, `canAll` (superuser short-circuit) and `isVendorUser`.
- **`lib/query-client.ts`**: `staleTimes {reference, master, transactional, live, dashboard}` (22); `queryClient` with no retry on business errors (46); `queryKeys` (122).
- **Query-key reality**: newer features define **feature-local keys under a shared prefix**, for example `purchaseReturnKeys = { all: [...queryKeys.procurement.all, 'purchase-returns'], … }` (`features/procurement/purchase-return-model.ts:55`). Same pattern in `transporterKeys`, `mastersKeys`, `financeKeys` and others.

#### D3. Hooks and components

- **`hooks/use-list-query.ts:44`**: `useListQuery<T>({path, queryKey, filters, defaultLimit, defaultSortBy, defaultSortDir, withTotal, enabled, staleTime, refetchInterval, refetchOnWindowFocus})`. It keeps state in the URL (`page`, `limit`, `q`, `sort`, `dir`) and returns `data`, `pagination`, `setPage`, `setLimit`, `setSearch`, `setSort`, `setParam`, `setParams`.
- **Other hooks**: `use-item-attribute-options.ts` is the pattern for a picker. It uses `LOOKUP_LIST_LIMIT`, `staleTimes.reference` and `emptyOnPermissionError`. `use-organisation-options`, `use-user-options`, `use-debounced-value` also exist.
- **`components/data-table.tsx:100`**: `DataTable<T>` with `manualPagination` and `manualSorting`. Props: `columns`, `data`, `pagination`, `isLoading`, `isFetching`, `error`, `onPageChange/onLimitChange/onSortChange/onSearchChange/onRefresh`, `searchValue`, `toolbar`, `onRowClick`, `getRowId`, `empty*`, `virtualise`. Mark non-whitelisted columns `enableSorting: false`.
- **`components/shared.tsx`**: `PageHeader({title, description, breadcrumbs, actions, status})` (52), `STATUS_VARIANTS` (112–218), `StatusBadge` (220), `StatTile` (232), `ConfirmDialog` (309), `DescriptionList` (372), `DateRangeFilter` (435), `FieldError` (495), `EmptyState` (507).

#### D4. Libraries and types

- **`lib/constants.ts`**: the only reader of `import.meta.env` (apart from one `import.meta.env.DEV` in `error-boundary.tsx`). Holds `PAGE_SIZES`, `DEFAULT_PAGE_SIZE=50`, `PICKER_PAGE_SIZE=20`, `LOOKUP_LIST_LIMIT=200`, `SEARCH_DEBOUNCE_MS=300`, `SELECT_ALL/SELECT_NONE`, `REASON_MIN/MAX_LENGTH`, date formats and locale.
- **`lib/messages.ts`**: `success.created(noun)` and the other standard verbs (23–51), `errors` (55), `errorTitle`, `errorDescription`, `empty`, `prompts`.
- **`lib/api-client.ts`**: `api.get/getPage/post/put/patch/delete/download(path, filename?)` (266, 305). `ApiError` (28). `emptyOnPermissionError` (68). This is the only place `fetch` is called.
- **`types/api.ts`**: `SchemaOf<'Name'>` (27), `PaginatedEnvelope`, `ListParams`, `DecimalString`. `types/openapi.ts` is generated, about 2.5 MB.

#### D5. Forms, dialogs and responsive conventions

- **Form and dialog precedent**: `features/masters/attribute-dialogs.tsx`.
  - zod schema; any `refine` predicate is annotated `(v): boolean`.
  - `useForm({ resolver: zodResolver })`, then `reset` on open.
  - `useMutation` calling `api.post` / `api.patch`; on success, invalidate the feature keys plus any shared picker keys, then `toast.success(success.created('X'), code)`; on error, `applyFieldErrors(error, setError)`.
  - Uses `Dialog / DialogContent / DialogHeader / DialogBody / DialogFooter` from `@/components/ui/overlays` and `Button` with its `loading` prop.
- **Responsive rules** (`docs/architecture.md`, frontend section): toolbars use `flex-wrap` with `w-full sm:w-[..]` controls; `DialogContent` scrolls its own overflow; no `shrink-0` on a wrapping group.

#### D6. Frontend tests

- **`src/test/setup.ts`**: jest-dom, `matchMedia`, `ResizeObserver`, pointer-capture and `scrollIntoView` stubs, and the `:modal`/`:fullscreen`/`:popover-open` shim.
- **`src/test/harness.tsx`**: `renderScreen(ui, {permissions, routes: {'GET /path': page([...])}, route, path, user, allowUnrouted})` (227). Helpers `page()`, `ok()`, `fail(status, code, msg, details)`. It stubs `fetch` (an unrouted call throws) and returns `{fetch: {calls, requestFor}, user, queryClient}`.
- **Small examples to copy**: `features/finance/finance-page.test.tsx` (62 lines) and `features/masters/attribute-dialogs.test.tsx` (145 lines). `features/transporters` and `features/warehouses` have no tests.
- **Fuller example**: `features/procurement/purchase-returns-page.test.tsx`.
- **Bundle budget** (`scripts/check-bundle-budget.mjs`, run in `npm run build`): each route chunk must be ≤ 20 KB gzipped, its closure ≤ 40 KB, and first load ≤ 210 KB.

### E. Contract pipeline

- **`make contract` / `.\make.ps1 contract`**:
  1. `python -m scripts.export_openapi` builds the app via `register_module_routers()` and writes a sorted `openapi.json`.
  2. `npm run contract:types` runs `openapi-typescript ../../openapi.json -o src/types/openapi.ts`.
  3. `python -m scripts.export_postman` writes `postman_collection.json`, one folder per tag.
- **`contract-check`**: the same three steps with `--check`, plus `node scripts/check-contract-types.mjs`.
- **`make check`** = `contract-check` + `lint` + `test` + `test-web`.
- **CI (`.github/workflows/ci.yml`)**:
  - **backend-quality**: ruff, mypy, both `--check` exporters, unit tests.
  - **backend-integration**: `alembic upgrade head`, then integration and API tests.
  - **frontend**: lint, format, typecheck, `contract:check`, vitest, build.
  - **e2e**: prod compose up, migrate, seed with the bootstrap password, Playwright.
- **Current contract**: 754 schemas (29 with module-qualified names), 365 paths, 494 operations, 27 tags.

### F. Docs and hooks

- **`.pre-commit-config.yaml`**
  - **pre-commit** (staged files): file hygiene (large-file limit 1024 KB on added files), `forbid-env-files`, `forbid-runtime-storage`, ruff format and check, prettier, eslint, `bash -n`.
  - **pre-push** (whole tree): mypy, unit pytest, OpenAPI `--check`, Postman `--check`, `tsc -b --noEmit`, `contract:check`, vitest, compose config.
- **Docs to touch for a feature**:
  - `docs/database.md`: §2 module and table map (49–77), §7 state machines (410), §8 cross-module events (440).
  - `docs/assumptions.md`: entries are numbered `## N. Title` with **Assumed / Why / Where / Impact if wrong**, separated by `---`. Next number is **39**. Open questions follow. Add an `ASSUMPTION:` comment in code at the decision point.
  - `docs/api.md`: request count (28) and endpoint map (375).
  - `docs/architecture.md`: Modules (135) and dependency direction (152).
  - `docs/security.md`: §4 vendor portal.
  - `docs/handbook/content.json` (structure: `frame`, `chapters[] {groupTitle, groupIntro, screens[] {route, title, purpose, whoUses, keyActions, flow, rules, permission, notes}}`), plus `tests/e2e/scripts/capture-screens.mjs` `SCREENS` (89) and `build-handbook.mjs`.

### G. Demo data (`apps/api/src/core/demo_data/`)

- **Stages**: `STAGE_ORDER` is masters, engineering, procurement, operations, finance, documents (`__init__.py`). Each stage module exposes `async def seed(ctx: DemoContext)`, loaded lazily by `_load_stage`.
- **`DemoContext`** offers `remember`, `id_of`, `maybe_id`, `has`, `note(stage, created, existing)` and `code(...)`, which produces `"DM-…"` (`DEMO_PREFIX="DM"`).
- **Idempotence**: `find_by_code(session, model, company_id, code)`, keyed on identity and never on the clock.
- **Execution**: the whole run is one transaction, flushed per stage. It acts as the seeded admin with every permission. Rows go through the services (the item-attribute precedent `masters.py:1019` uses `repo.add` directly).
- **Run**: `make demo-data`, or `python -m src.core.demo_data --only masters,...`.

### H. End-to-end tests (`tests/e2e`)

- **Setup**: Playwright 1.49, `playwright.config.ts` (testDir `specs`, `workers: 1`, serial). Project `setup` → `specs/auth.setup.ts` → `.auth/admin.json`.
- **Configuration**: `lib/env.mjs` loads `tests/e2e/.env` (`E2E_BASE_URL`, `E2E_API_URL`, `E2E_ADMIN_EMAIL=admin@atcraft.example`, `E2E_ADMIN_PASSWORD`, `E2E_BOOTSTRAP_PASSWORD`).
- **Login**: `lib/session.mjs` provides `readyAdminToken()`, which signs in with the bootstrap password and completes the forced password change, and `signInThroughForm(page)`.
- **Journey shape**: `order-to-receipt.spec.ts` (API-driven, 6 tests) and `ui-write-paths.spec.ts` (form-driven, 5 tests). Each uses one shared `Page` in `beforeAll`, because refresh-token rotation makes a replayed token fail. A per-run `RUN` suffix keeps codes unique. Every write is asserted back through the API.
- **Sweeps** (`scripts/*.mjs`, not run in CI): browser, a11y, responsive, theme, permission, data, tenant-isolation.

---

## Missing vs proposal (checked against the code in this area)

| # | Audit claim | What actually exists | What is missing (cross-cutting registrations) |
|---|---|---|---|
| 1 | Public dealer site / mobile API | Dealers are `customers` with `CustomerType.DEALER/DISTRIBUTOR` (`customers/models.py:66-78`), `dealer.view/manage`, a `/dealers` page. | No customer or dealer principal: `Principal` has `vendor_id` but no `customer_id`. No public, unauthenticated routes besides `/auth/*`. No cart, wishlist, banner or offer tables or permissions. **Confirmed absent.** |
| 2 | Vendor mobile app and push | Vendor portal = the same SPA with `VENDOR_*` roles and `isVendorUser`. | `NotificationChannel` is IN_APP and EMAIL only. No device-token table, push provider, or SMS/WhatsApp settings. **Confirmed absent.** |
| 3 | Sales invoice + PDF | **Partly scaffolded**: `Permission.SALES_INVOICE_VIEW/CREATE` (`permissions.py:116-117`, in the `_SALES` and `_ACCOUNTS` bundles and in `PERMISSION_GROUPS["Sales"]`); `P.SALES_INVOICE_*` in the frontend; `DocTypes.SALES_INVOICE = DocType("sales_invoice","INV")` (`codes.py:67`); `SalesOrderStatus.INVOICED` with the edge `DISPATCHED→INVOICED` gated on `SALES_INVOICE_CREATE` (`sales/service.py:206-218`); `INVOICED` in `STATUS_VARIANTS`; reportlab plus the packing-list PDF job pipeline; `docs/assumptions.md` §32 records why it is absent. | No table, service, route or UI. **No route moves a sales order to INVOICED or CLOSED** (only the machine names them), so orders stop at DISPATCHED. |
| 4 | Customer return | **Inventory seam exists**: `TransactionType.CUSTOMER_RETURN` with sign +1 (`inventory/models.py:118,148`); `_Transition(None, AVAILABLE, "target")` (`inventory/service.py:373`); serial → `RETURNED` (1710); `StockState.RETURN`; seeded `WH-RET` of type `RETURN`; the `items.can_return` flag. | No caller posts `CUSTOMER_RETURN`. No DocType, permission, `*Status` enum, tables, routes, UI or report. |
| 5 | Cost-centre master | Nothing. Closest analogue: `departments` (organization module, `DEPARTMENT_LIFECYCLE`, tab on `/admin/organisation`). | Everything: table, permission and group, migration, UI. |
| 6 | Brand / manufacturer masters | Nothing (grep found none). `item_attributes` (FINISH/SIZE/COLOUR) is the precedent. | Everything. |
| 7 | Job-work invoice | Job-work charges are tracked; `VendorInvoice` with the `VINV` series. | No DocType, permission or document of its own. |
| 8 | Auto-split requirement into a PO per category | MRP `convert_suggestion` (`mrp/service.py:1110`); requisition and PO creation. | Nothing cross-cutting beyond procurement. |
| 9 | Timed alerts | One sweep: `notifications.scan_dispatch_delays` (`celery_app.py:87-91`, its comment wrongly says "purchase order"), which scans outbound dispatches only. `NotificationType` declares `PURCHASE_ORDER_DELAYED`, `JOB_WORK_OVERDUE`, `PAYMENT_DUE`, `APPROVAL_PENDING`, `GOODS_RECEIPT_POSTED` and others. | **Only 4 of the 20 types are ever raised.** No PO-not-acknowledged, tracking-ID-missing or GRN-not-created sweeps. |
| 10 | Dealer performance + payment follow-up | `ReportKey.VENDOR_PERFORMANCE` (`reports/models.py:70`); outstanding and ageing in finance. | No dealer or customer report key. That needs a new `ReportKey`, a `REPORT_CATALOGUE` entry, a route, **and a CHECK-swap migration**. No follow-up workflow or document. |
| 11 | Shift roster | `work_orders.shift` and `production_entries.shift` are free-text `String(16)` (`production/models.py:490-492, 758`). | No shift master, roster table or permission. |

---

## Patterns to copy

1. **A plain master** (cost centre, brand, manufacturer). Copy the item-attribute master end to end. It is the most recent and complete precedent.
   - **Model**: `ItemAttribute(TenantMasterEntity)` (`products/models.py:274-325`). `code String(32)`, `name`, `sort_order`, `is_active`. `UniqueConstraint("company_id", …, "code", name="uq_…")`. CHECK built with `sql_in_list`. A partial picker index (`is_active AND deleted_at IS NULL`).
   - **Schemas**: `ItemAttributeCreate`, `ItemAttributeUpdate` (every field optional, no immutable fields), `ItemAttributeOut` (`products/schemas.py:128-163`). Codes pass through `normalise_code` in a `field_validator`.
   - **Repository**: `ItemAttributeRepository` (`products/repository.py:290-330`) with `entity_label`, `searchable_fields`, `sortable_fields`, `get_by_code`, `list_page_search`, `is_used`.
   - **Service** (`products/service.py:1495-1593`): `async with transaction(session)` → duplicate check → `repo.add` → `flush` → `record_audit(CREATE, entity_type=AUDIT_ITEM_ATTRIBUTE, new_values=snapshot(...))`. Update snapshots before and after. Retiring is refused while the row is in use.
   - **Reference validation**: `_require_attribute` (1073) checks tenant, kind and active state on every FK assignment.
   - **Router** (`products/router.py:185-229`):
     ```python
     @router.get("/attributes", response_model=PaginatedResponse[ItemAttributeOut],
                 dependencies=[require_permission(Permission.ITEM_ATTRIBUTE_VIEW)])
     ```
     plus POST (201) and PATCH, returning `ApiResponse.ok(x, "Attribute created")`.
   - **Permission**: enum entries `ITEM_ATTRIBUTE_VIEW/MANAGE`, a `PERMISSION_GROUPS` entry, the `DESIGN_ENGINEER` preset, frontend `P`, and grant migration 0902.
   - **Migration**: 0900.
   - **Frontend**: a tab in `features/masters/masters-page.tsx` (`tabs` array at 1308), `attribute-dialogs.tsx` + test, the hook `use-item-attribute-options.ts`, `mastersKeys`.
   - **Demo data**: `masters.py:_seed_item_attributes`.
   - **Alternatives**: for a master with its own page and API tag, copy Transporter (sub-router `transporters_router` included in `vendors/router.py:79, 811-812`, `TRANSPORTER_LIFECYCLE` at `vendors/service.py:407`, `features/transporters/*`). For an organisation-level master such as a cost centre, copy `Department` with `_lifecycle()` and the `/admin/organisation` tab (`organisation-page.tsx:922-1011`).

2. **A document with lines and a state machine** (sales invoice, customer return, job-work invoice). Copy the purchase return.
   - **Model**: `PurchaseReturn(TenantEntity)` (`procurement/models.py:1336-1415`).
     - `code String(64)` unique per company; FKs `RESTRICT`.
     - `status String(24)` with default and server default `DRAFT`.
     - Money columns `Money` with `Decimal("0")` defaults; currency from `DEFAULT_CURRENCY`.
     - `posted_at`, `posted_by_id`, `cancellation_reason`.
     - CHECKs: `status_known` (via `sql_in_list`), `posted_returns_carry_evidence`, `cancelled_returns_carry_a_reason`.
     - Indexes on `(company_id, status, date)`.
     - Lines live in `PurchaseReturnLine(TenantEntity)`.
   - **Machine**: `PURCHASE_RETURN_LIFECYCLE` (`procurement/service_rules.py:408-427`).
   - **Service** (`procurement/service.py`): `create_purchase_return` (3383) takes the next number with `next_document_number(session, tenant.company_id, DocTypes.X, on=return_date)` inside `transaction`. `post_purchase_return` (3533) posts the inventory movements. `cancel_purchase_return` (3706) does `lock_for_update` then `_transition(..., action=AuditAction.CANCEL)`. Detail is built from separately loaded lines with `return_detail(purchase_return, lines)`, not from a relationship.
   - **Router** (`procurement/router.py:1150-1237`): list (filters, `PageDep`, `SortDep`), create (201), get, `/{id}/post`, `/{id}/cancel` with a `ReasonRequest`.
   - **Repository**: `PurchaseReturnRepository` (`sortable_fields`, `_filtered` including the vendor-portal clause, `list_page_filtered` using `sort.resolve(...)`).
   - **Frontend**:
     - `purchase-return-model.ts`: `SchemaOf` types, keys under a shared prefix, a statuses list with `satisfies`, transition predicates mirroring the machine.
     - `purchase-returns-page.tsx`: `useListQuery`, `DataTable`, `PageHeader`, `StatusBadge`, `can(P.X)`.
     - `purchase-return-dialogs.tsx`.
     - `purchase-returns-page.test.tsx`.
   - **Concurrent editing**: add `OptimisticLockMixin` if several users edit the document, as `SalesOrder` and `VendorInvoice` do.

3. **A PDF of a document**: copy the packing list (A13). Use a document-only `ReportKey` plus a CHECK-swap migration (precedent `5e2b8c41f7a3`), a `@async_task(name="reports.generate_<x>", queue="reports")` task, and `DOCUMENT_ONLY_REPORT_KEYS`. On the frontend, `useReportJob()` and `ReportJobPanel` (`features/reports/report-jobs.ts:107`, `job-panel.tsx:21`) poll the job, then `api.download('/documents/{id}/download', name)` fetches the file (`packing/packing-order-dialog.tsx:924-974`).

4. **A timed alert**: copy `dispatch.service.scan_delays` (`dispatch/service.py:1846`).
   - Read across tenants, then partition by company.
   - Write each company's alert in its own nested `transaction`, so one tenant's failure does not stop the others.
   - Call `notify_role(..., role_codes=DELAY_ALERT_ROLES, group_key=...)`.
   - Write one audit row per company per sweep.
   - Wire it up with a thin Celery adapter (`workers/tasks/notifications.py:60`) and a `beat_schedule` entry with `expires`.

5. **A report**: add a `ReportDefinition` to `REPORT_CATALOGUE` (`reports/service.py:538`; `VENDOR_PERFORMANCE` is at 614), a route at the kebab-case of the key (`reports/router.py:609-632`, `response_model=None`, `REPORT_VIEW`), the `ReportKey` member plus a CHECK-swap migration, and the frontend `ReportKey` union (`features/reports/reports-types.ts:23-43`).

6. **A new permission**: enum entry + `PERMISSION_GROUPS` + presets + `P` + a 0902-style data migration + the route decorator + the UI gate + nav/route permissions + the permission-sweep entry.

7. **A new module package**: `src/modules/<name>/{__init__,models,schemas,repository,service,router}.py`. In `router.py`: `router = APIRouter(prefix="/x", tags=["X"], responses=COMMON_ERROR_RESPONSES)`. Add the name to `MODULE_ORDER`. Respect the dependency direction in `docs/architecture.md:152`.

8. **A new screen**: page file with a default export → `routeLoaders` → `lazy()` and a `routeGroups` entry → a `navigation` item → the four e2e sweep lists → `capture-screens` and `content.json`.

---

## Integration points

- **Transactions and numbering**: `src.core.database.transaction`; `src.common.codes.next_document_number` plus a `DocTypes` constant.
- **Audit**: `src.modules.audit.service.record_audit` / `record_status_change` / `snapshot`, in the same transaction.
- **Inventory**: `src.modules.inventory.service.post_movement(session, tenant, *, item_id, warehouse_id, quantity, transaction_type, reference_type, reference_id, reference_code, reason, location_id=None, batch_id=None, serial_id=None, unit_cost=None, stock_state=AVAILABLE, occurred_at=None)` (3301), `post_movements` (3345), `change_stock_state`, `transfer_stock`. A customer return uses `CUSTOMER_RETURN`; dispatch uses `DISPATCH`.
- **Allocation**: `src.modules.allocation.service.reserve / release / consume`.
- **Finance**:
  - `post_journal(session, tenant, lines, *, entry_date, reference_type, reference_id, reference_code, narration, currency, journal_id)` (`finance/service.py:1158`) is the **only way into `ledger_entries`**. `reverse_journal` (1274).
  - `post_customer_adjustment(session, tenant, principal, customer_id, payload)` (3189) moves `customers.outstanding_amount` together with a receivable journal. Its route is gated on `payment.approve`.
  - `ensure_system_accounts` (920). Ageing and outstanding read the ledger.
- **Sales**: `SALES_ORDER_LIFECYCLE` `DISPATCHED→INVOICED→CLOSED` through `sales/service.py:_transition` (1825).
- **Notifications**: `notify` / `notify_role` plus the `NotificationType` member plus the frontend `NOTIFICATION_TYPES` (`features/notifications/notification-types.ts:27-48`).
- **Reports and documents**: `reports.service.new_job_snapshot / dispatch_job / start_snapshot / complete_snapshot / fail_snapshot / job_status_url`; `documents` (`Document`, `DocumentCategory.REPORT`, `KNOWN_TYPES`, `build_storage_key`, `resolve_storage_path`, `default_expiry`, `DocumentRepository`). `documents.entity_type` is free snake_case, at most 48 characters.
- **Celery**: `celery_app.include`, `task_routes` (by name prefix), `beat_schedule`, `MAIL_TASKS`. Worker queues differ by stack:
  - prod: `-Q default,mrp,reports` plus a separate `notifications` worker (`docker-compose.prod.yml:83`)
  - base and dev: the worker has **no `-Q`** (`docker-compose.yml:250`, `docker-compose.dev.yml:66`)
- **Users and roles**: the permission catalogue drives the role editor automatically. `assert_vendor_role_consistency` applies to any new vendor role.
- **Frontend**: `useAuth().can`, `P`, feature-local key factories, `useListQuery`, `DataTable`, shared components, `applyFieldErrors`, `success.*`, `staleTimes`, `api.download`.

---

## Traps & constraints

1. **Migrations must stay one linear chain from `2f5e08c7ba61`.** Features built in parallel that each branch from HEAD create multiple heads, and `alembic upgrade head` (CI, deploy) then fails. Serialise the `down_revision` values or add a merge revision.
2. **Autogenerate output must be hand-cleaned.** It renders `src.core.types.GUID()`, `StatusEnum(...)` and `CIText(...)`, and at least `StatusEnum` raises `TypeError` on import (`initial_schema.py` docstring item 4). Rewrite them as `sa.Uuid()` and `sa.String(n)`.
3. **Alembic does not detect CHECK-text changes.** Adding a member to any enum used in a `sql_in_list` CHECK (`ReportKey`, statuses, `DispatchType`, `PartyType`, etc.) needs a hand-written drop / recreate `NOT VALID` / validate migration with literal values. Tests build from metadata, so the suite passes while production rejects the insert with 422 `BUSINESS_RULE_VIOLATION`.
4. **No test detects a missing migration.** There is no `alembic check`, and pytest uses `create_all`. Only CI's `alembic upgrade head` and the e2e journeys run against a migrated schema.
5. **Triggers are invisible to metadata.** A new append-only table needs `CREATE TRIGGER … EXECUTE FUNCTION atcraft_reject_row_mutation()` written by hand.
6. **New permissions do not reach deployed roles.** See A5: deploy does not seed, and non-system roles are client-owned. Superusers mask gating bugs during manual testing; use `permission-sweep.mjs` and `limited_principal`.
7. **`Transition.permission` is not enforced** by `assert_transition`. The route decorator is the gate. The frontend's `isPostable`-style predicates must mirror the machine (CLAUDE.md trap on the stranded `QC_PENDING`).
8. **The gate-test parser has rules**: write `Permission.X` literally in the decorator (the financial-year routes use `FY_VIEW` and are therefore unparsed), use literal paths and literal `APIRouter(prefix=...)`, and keep UI files in the folder that names `P.X`.
9. **OpenAPI schema-name collisions.** Two Pydantic classes with the same name in different modules become `src__modules__<mod>__schemas__<Name>`; 29 already exist, for example `src__modules__procurement__schemas__ReasonRequest`. A new class reusing a name that is currently unique **renames the existing schema** and breaks every `SchemaOf<'Name'>`. Use module-specific names such as `SalesInvoiceSummary`.
10. **`response_model=None`** (used for responses that vary by permission) emits an empty schema. Such endpoints need hand-written mirrors registered in `MIRRORS`.
11. **CLAUDE.md traps that apply directly:**
    - `model_validate(orm_row)` reads `lazy="raise"` relationships; build the detail from separately loaded lines, or use `set_committed_value`.
    - `clause or default` raises; use `sort.resolve`. `list_page` takes one order clause (`products/repository.py:320-326`).
    - `if xs:` with no `else` drops the filter.
    - Order by `(created_at, id)`.
    - Never call `session.rollback()` in a service. Hold ids before an expected failure.
    - Demote before promote under partial unique indexes.
    - A UUID FK is not a tenant check.
    - Use Python-side `onupdate` only.
    - Every sortable column needs a `sortable_fields` entry.
    - Every new status needs a `STATUS_VARIANTS` entry.
12. **A plain `String` status column returns `str`, not the enum.** Use `==` or wrap with `status_enum(entity.status)`. Inventory's `StatusEnum` exists for this reason (`inventory/models.py:318`).
13. **Vendor-portal scope is not structural.** `_scoped` applies `company_id` only. `tenant.vendor_id` filters exist only in procurement, vendors, users and products, yet `VENDOR_OWNER` and `VENDOR_ACCOUNT_MANAGER` hold `vendor_invoice.view/create`, `payment.view`, `ledger.view`, `job_work.view`, `qc.view` and `dispatch.*`. **Finance, job_work, dispatch and quality repositories contain no `tenant.vendor_id` predicate** (grep), while `docs/security.md` §4 says vendor users "are scoped to their own vendor's records". Any vendor-facing feature (job-work invoice, a vendor app) must add the predicate in its repository, plus an HTTP test modelled on `tests/api/test_vendor_portal_scope_http.py`.
14. **Numbering ignores `companies.financial_year_start_month`.** No caller of `next_document_number` passes `fy_start_month`, so April is hard-wired despite `docs/assumptions.md` §13. Prefixes are not checked for uniqueness. The sales-invoice prefix is `INV` in `DocTypes`, while a frontend fixture shows `SINV/...` (`accounts-page.test.tsx:88`); choose deliberately.
15. **Invoice transition rights**: `DISPATCHED→INVOICED` requires `sales_invoice.create`. `SALES_EXECUTIVE` holds only view; `ACCOUNTS`, `ADMIN` and `SUPER_ADMIN` hold create.
16. **New Celery tasks.**
    - Name the task with a routed prefix and add the module to `include`.
    - In base and dev compose the worker passes no `-Q`, so it consumes only `task_default_queue` (`default`). By Celery's default behaviour, `reports.*` and `mrp.*` tasks are consumed only by the prod worker. Verify before relying on a reports-queue job in dev.
    - Tasks must be idempotent because `task_acks_late=True`.
    - Use `group_key` to collapse repeated alerts.
17. **New notification types** need no migration (there is no CHECK on `notification_type`), but the hand-written frontend `NOTIFICATION_TYPES` must be updated, and no guard enforces it.
18. **Frontend limits.** A route chunk must stay ≤ 20 KB gzipped, so split large dialogs. The `limit: 20`/`200` and `300 ms` literals are banned even in tests. Annotate zod `refine` predicates `(v): boolean`. Do not add private jsdom shims.
19. **`/masters` gating.** The route and nav item allow `[CATEGORY_VIEW, UOM_VIEW, PRICE_VIEW]` but not `ITEM_ATTRIBUTE_VIEW` (`router.tsx:348-352`, `navigation.ts:124-129`). A new master tab's right must be added to both lists, or holders of only that right cannot reach the page.
20. **Route ordering.** Declare static sub-paths (`/options`, `/permissions/catalogue`) before `/{id}` (`users/router.py:19`).
21. **Hot shared files.** Expect merge conflicts if features land in parallel:
    - `core/permissions.py`, `lib/permissions.ts`, `common/codes.py`, `api/router.py`
    - `router.tsx`, `route-loaders.ts`, `navigation.ts`, `components/shared.tsx` (`STATUS_VARIANTS`)
    - `celery_app.py`, `notifications/models.py` plus `notification-types.ts`, `reports/models.py` plus `reports-types.ts`
    - the four e2e sweep scripts
    - `docs/api.md` (count), `docs/assumptions.md` (numbering), `docs/database.md`
    - the regenerated `openapi.json`, `types/openapi.ts` and `postman_collection.json`
    - the single migration chain
22. **Windows box specifics.** Run Python tools via `apps/api/.venv/Scripts/python.exe -m …` and `.\make.ps1 <target>`. Infrastructure is at 127.0.0.1:5433 (Postgres) and 127.0.0.1:6380 (Redis). The pre-push vitest run alone takes about 14 minutes.

---

## Files a change here will touch

**Backend (always)**
- `apps/api/src/modules/<module>/{models,schemas,repository,service,router}.py`. For a new package, also `__init__.py` and an entry in `apps/api/src/api/router.py` `MODULE_ORDER`.
- `apps/api/src/core/permissions.py`: `Permission`, `PERMISSION_GROUPS`, bundles and `ROLE_PRESETS`.
- `apps/api/src/common/codes.py`: `DocTypes`, for numbered documents.

**Backend (when applicable)**
- `apps/api/src/core/messages.py`: only for sentences shared by two or more modules.
- `apps/api/src/core/constants.py`
- `apps/api/src/core/seed.py`: new baseline rows.
- `apps/api/src/modules/notifications/models.py` (`NotificationType`); `apps/api/src/workers/celery_app.py` (`include`, `beat_schedule`); `apps/api/src/workers/tasks/{notifications,reports}.py`
- `apps/api/src/modules/reports/{models,service,router,schemas}.py`
- `apps/api/src/modules/sales/service.py`: the `INVOICED` transition.
- `inventory`, `finance`, `allocation`, `documents`: callers only.
- `apps/api/src/core/config.py` together with `.env.example`, when new settings are added.

**Migrations**
- `apps/api/migrations/versions/2026MMDD_HHMM_<slug>.py` with `down_revision = "2f5e08c7ba61"` or the latest in the chain, with the review checklist filled in.
- Plus a 0902-style grant migration for new permissions.
- Plus a CHECK-swap migration for any enum change.
- Plus a trigger migration for any new append-only table.

**Tests**
- `apps/api/tests/unit/test_<module>_lifecycle.py` and pure rule tests.
- `apps/api/tests/integration/test_<module>_service.py`, using the module-scoped schema and `tests/seed_support`.
- `apps/api/tests/api/test_<x>_http.py`, for permission decisions and vendor scope.
- Optionally a migration round-trip test modelled on `test_mrp_late_supply_migration.py`.
- Registry entries where needed: `MIRRORS` in `test_frontend_contract_types.py`; `_EXTRA_MODULES` in `test_state_machine_entity_names.py`.

**Contract artefacts (regenerate with `make contract`)**
- `openapi.json`, `apps/web/src/types/openapi.ts`, `postman_collection.json`.

**Frontend**
- `apps/web/src/features/<feature>/{<x>-model.ts, <x>-page.tsx, <x>-dialogs.tsx, <x>-page.test.tsx}`
- `apps/web/src/app/{route-loaders.ts, router.tsx, navigation.ts}`
- `apps/web/src/lib/permissions.ts`
- `apps/web/src/components/shared.tsx` (`STATUS_VARIANTS`)
- Optionally `apps/web/src/lib/query-client.ts` (shared prefix), `apps/web/src/lib/messages.ts`, `apps/web/src/hooks/use-<x>-options.ts`, `features/notifications/notification-types.ts`, `features/reports/reports-types.ts`, and the `/masters` or `/admin/organisation` tab arrays.

**E2E and handbook**
- `tests/e2e/scripts/{browser-sweep,a11y-sweep,responsive-sweep,permission-sweep}.mjs` (and `theme-sweep` if desired), `tests/e2e/specs/*.spec.ts` for a new journey, `tests/e2e/scripts/capture-screens.mjs` (`SCREENS`), `docs/handbook/content.json`.

**Demo data**
- `apps/api/src/core/demo_data/<stage>.py`, either an existing stage's `seed(ctx)` or a new module added to `STAGE_ORDER`.

**Docs**
- `docs/database.md` (§2 map, §7 machines, §8 events)
- `docs/assumptions.md` (§39 onward, plus `ASSUMPTION:` comments in code)
- `docs/api.md` (`"N requests in F folders"` at line 28, plus the endpoint map)
- `docs/architecture.md` (modules and dependency direction)
- `docs/security.md` §4, if vendor scope changes
- `README.md` and `apps/api/README.md` only if layout or commands change
