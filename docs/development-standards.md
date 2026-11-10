# Development and completion standards

These standards apply to existing modules and new work. The binding engineering
rules remain in [CLAUDE.md](../CLAUDE.md); the
[database contract](database.md) defines ownership and service boundaries.
Refactor through the existing shared implementations and preserve documented
business behavior, permissions, API compatibility and transactional guarantees.

## Shared implementation points

| Concern | Implementation and rule |
|---|---|
| HTTP contracts | `apps/api/src/core/responses.py`, `errors.py`, `exception_handlers.py` and `validation.py`; use typed envelopes, stable error codes, field errors and the status conventions in [api.md](api.md). Regenerate OpenAPI, frontend types and Postman together. |
| Request boundaries | Pydantic request/query schemas, route permission dependencies and shared middleware. Bound input sizes and pagination; derive tenant and portal scope from the verified principal. A request body forbids keys it does not define (`extra="forbid"`), and a partial update calls `require_changes` (`core/validation.py`) before it reads anything. An audited reason - a field, alias, query or `len()` check - takes its floor from `REASON_MIN_LENGTH` (`core/constants.py`), never a number (`tests/unit/test_reason_minimum.py`), and a validator that refuses one says `REASON_TOO_SHORT` (`core/messages.py`). An update field that may be left out but never cleared refuses an explicit null with `reject_null` (`core/validation.py`). Every numeric input has an upper bound - an integer column's is `MAX_INTEGER_COLUMN` (`core/constants.py`) - and every string a length that fits its column (`test_request_numeric_bounds.py`, `test_request_text_bounds.py`, `test_request_lengths_fit_columns.py`). File downloads retain their binary contract. |
| Authorization, approvals and budgets | Every route depends on `require_permission`, or is allow-listed with its reason in `tests/unit/test_route_authorization.py`. A staff route that a portal's allow-list admits refuses that portal's users with `forbid_vendor_users` or `forbid_dealer_users` (`core/deps.py`; `test_portal_route_scope.py`). Who may approve their own document is decided by `can_approve` in `apps/api/src/common/approvals.py` and refused with `SelfApprovalError` (`core/errors.py`; `test_self_approval.py`); who is exempt in each module is [assumptions.md](assumptions.md) section 2. A rate limit is a named `RateLimitPolicy` (`core/ratelimit.py`) spent by `spend_budget` (`core/deps.py`) or `enforce_policy`: an upload spends `FILE_UPLOAD`, a queued PDF or export `DOCUMENT_PDF` or `REPORT_EXPORT`, and every policy is spent somewhere (`test_route_budgets.py`); only a `*_PER_IP` policy is keyed on an address, and it always is (`test_ratelimit_policies.py`). |
| Business rules | Thin routers, services for authorization-sensitive business decisions and transactions, tenant-scoped repositories for persistence. Lock and re-read mutable inventory; keep movements, document transitions and audit writes atomic. A status changes only after `StateMachine.assert_transition` (`core/state_machine.py`), which also words the refusal: an operation that leaves from one state only - a restore, a recount - passes `source=` rather than building `InvalidStateTransitionError` itself. A self-referencing master (department, cost centre, warehouse, storage location) checks a new parent with `assert_parent_allowed` in `common/hierarchy.py`. |
| UI system | `apps/web/src/styles/globals.css` and `src/components/ui`; reuse token colors, typography, spacing, buttons, fields, dialogs and overlays. A colour is a token, never a white, black, palette or arbitrary utility (`tests/unit/test_frontend_colour_tokens.py`). Lists use `DataTable` and `useListQuery`. |
| Controls and headings | `Button` (`components/ui/button.tsx`), with `variant="destructive-outline"` rather than a destructive class on another variant (ESLint `destructiveButtonClassName`); `CellButton` (`components/data-table.tsx`) for a cell that opens or acts on its row, and `FilterChip` (`components/shared.tsx`) for a filter a list is narrowed by - a raw `<button>` in a screen is counted against a reasoned allowance (`test_frontend_raw_buttons.py`). `NativeSelect` and `Checkbox` (`components/ui/primitives.tsx`; ESLint `nativeSelect`, `nativeCheckbox`); an optional Radix `Select` (`components/ui/select.tsx`) offers "none" as `SELECT_NONE` (`lib/constants.ts`). `FieldError` (`components/field-error.tsx`) is the error line under a control (`test_frontend_load_errors.py`). `PageHeader` heads a page and `SectionHeader` (`components/shared.tsx`) each section, at one size (`test_frontend_section_headings.py`); a date or time field never shares a row on a phone (`test_frontend_form_grids.py`). |
| Pickers, tables and charts | `SearchPicker` (`components/ui/search-picker.tsx`) for a server search and `MultiSelectList` (`components/ui/multi-select-list.tsx`) for a checklist are the only listboxes (`test_frontend_listboxes.py`). A table outside `DataTable` draws its cells from the parts in `components/ui/table.tsx` and scrolls inside `TableScroller`, a named, focusable region (`test_frontend_tables.py`). A chart takes its axis tick, tooltip and screen-reader table from `components/chart.tsx` (`AXIS_TICK`, `ChartTooltip`, `ChartDataTable`). A status is a `StatusBadge` coloured from `STATUS_VARIANTS` (`components/shared.tsx`; `test_status_badge_catalogue.py`). |
| Dialogs and failure states | `ConfirmDialog` and `ReasonDialog` (`components/shared.tsx`): no screen declares its own reason dialog (`test_frontend_reason_dialogs.py`), a confirmation title is a question (`test_frontend_confirm_titles.py`) and a cancel, reject, retire, delete, remove or revoke confirmation is `destructive` (`test_frontend_destructive_confirms.py`); `window.alert`, `confirm` and `prompt` are refused (ESLint `no-alert`). A read that failed is a `LoadError`, and a panel the role may not read a `PanelDenied` (`components/shared.tsx`; `test_frontend_load_errors.py`). |
| Formatting, storage and exports | `apps/web/src/lib/format.ts` formats money, quantities, numbers, percentages and dates (`formatMoney`, `formatQuantity`, `formatDate`, ...), writes an empty value as `EMPTY_VALUE` and counts with `plural` (ESLint `utcDateSlice`, `decimalThroughNumber`, `localeFormatting`; `test_frontend_empty_value.py`, `test_frontend_plurals.py`). Browser storage goes through `readStorage`, `writeStorage` and `removeStorage` in `lib/storage.ts` (ESLint `browserStorageGlobals`, `browserStorageProperties`), a CSV download through `downloadCsv` in `lib/csv.ts` (`test_frontend_csv_exports.py`), and typed serial labels through `splitSerialNumbers` and `resolveSerialLabels` in `features/inventory/serial-numbers.ts` (`test_frontend_serial_labels.py`). |
| Messages and errors | Backend `core/messages.py`, and `referenced_in_use` in `core/errors.py` for every "something still depends on this record" refusal; frontend `lib/messages.ts`, `form-errors.ts`, `toast.ts` and `api-client.ts`. Show safe API messages and field errors; unexpected failures use a shared fallback and internal reporting. Preserve request correlation without exposing exception text, SQL, credentials or stack traces. A mutating route answers with a message, built by `success_message` for the standard verbs and never saying "successfully" (`tests/unit/test_success_messages.py`); a count goes through `plural`; a refusal is a sentence - capitalised, punctuated, never a list repr or a class name (`test_user_message_guards.py`), and one raised from two modules is written once in `core/messages.py` (`test_shared_message_catalogue.py`). On screen, a standard verb's confirmation comes from its `lib/messages.ts` builder (`test_frontend_success_builders.py`) and a failed mutation is announced once, by the query client's `MutationCache` (`test_frontend_mutation_error_toasts.py`). |
| Data loading | TanStack Query with shared `queryKeys` in `apps/web/src/lib/query-client.ts`: every key, and every feature factory's root, comes from that registry, so the prefix a write invalidates reaches every cache it changes (ESLint `literalQueryKey`). `createQueryClient` is the one client constructor (`queryClientConstruction`); a screen test that must see a cache outlive a mount uses `renderScreen`'s `cachePolicy: 'production'`. Forward cancellation signals: a query function destructures `{ signal }` and hands it to the api call or helper (`queryFnSignal`). Paginate on the server and preserve loading, empty and failure states. Bound retries and avoid duplicate requests or notification storms. |
| Logging and job failures | `apps/api/src/core/logging.py`, `errors.py`, `database_errors.py`, `observability.py` and `readiness.py`; `src/workers/celery_app.py`. A defect's traceback is logged once - `error.unhandled` in the API, `task.failed` in a worker - and a refusal (`is_refusal`: an `AppError` below 500) as an expected outcome, never as an error. Log ids, codes and types, not free text that can carry what a person typed: no refusal sentence, bound value, constraint `DETAIL` or mail exception (`MAIL_TASKS`); a database error of SQLSTATE class 22 or 23 is withheld from every traceback, `error=str(exc)`, Celery line and Sentry event by `database_error_redactor`, so log the exception itself rather than rewording it. A beat sweep isolates each unit and counts a skipped one with `count_sweep_failure(<task name>)`; a readiness probe reports why it is false through `note_unready`. |
| Security and performance | Follow [security.md](security.md) and [performance.md](performance.md). Batch reference queries, select only needed data, close resources and send long calculations to workers. Never cache inventory availability. Hash and verify passwords with `hash_password_async` and `verify_password_async` (`core/security.py`), and hand a PDF or XLSX renderer to `anyio.to_thread.run_sync` or `asyncio.to_thread`: no `async def` under `src/modules` calls `hash_password`, `verify_password` or a `render_*_pdf` / `render_*_xlsx` itself (`tests/unit/test_event_loop_guards.py`). An audit write inside a loop passes `flush=False`, and the transaction flushes once (`test_audit_flush_guard.py`). |
| Documents and generated files | The module that owns an entity type registers, in `apps/api/src/common/document_access.py`, the right that filing a document against it needs (`register_document_attach_policy`, checked by `assert_may_attach`) and the reader that proves an id is a record this company holds (`register_document_entity_resolver`, read through `document_entity_resolver`) (`test_document_attach_policy.py`, `test_document_entity_resolvers.py`). A PDF a worker files is named by `_safe_file_name` in `src/workers/tasks/reports.py` (`test_generated_document_names.py`). |
| Display labels | A read that names rows by id adds read-only `*_code` / `*_name` fields beside them (serial labels as `serial_numbers` aligned with `serial_ids`, built by `labels_in_order`), filled through `labels_by_id` in `apps/api/src/common/repository.py`: one tenant-scoped IN-query per referenced table per page or document, archived rows included, nothing read for an empty set. The shared builder every endpoint of that read uses fills them, so no response leaves them empty, and no screen fetches per row or a whole list to label one. |
| Backend test support | `apps/api/tests/**/*_support.py`: `seed_support` builds rows through the ORM, `schema_support` prepares the metadata, `statement_support` counts statements, and each domain's shared builders and fixtures sit beside them (`procurement_support`, `sales_invoice_support`, `job_work_support`, ...). A test module never imports another test module (`tests/unit/test_support_modules.py`); what two suites share moves to a support module, and a fixture is imported with the explicit `name as name` re-export. `tests/conftest.py` registers every support module for pytest's assert rewriting. |

Use Python `snake_case` for modules/functions/variables, `PascalCase` for classes
and `UPPER_SNAKE_CASE` for constants. Frontend files follow the existing
`kebab-case` convention, components/types use `PascalCase`, hooks use `use...`,
and values/functions use `camelCase`. Database tables are plural `snake_case`;
HTTP paths follow the established resource names. A naming cleanup must update
callers and generated contracts without silently breaking persisted data or
public URLs.

Document non-obvious algorithms, locking order, permission-dependent responses
and business assumptions at the decision point. Record changed assumptions in
[assumptions.md](assumptions.md); comments that restate an assignment add no value.

## Verification required for completion

Review every affected function, method, component and endpoint for applicable
tests. Cover successful behavior, boundary and malformed inputs, validation and
business-rule failures, authorization/tenant isolation, expected exceptions and
unexpected failures. For asynchronous code, include cancellation, cleanup and
retry behavior where applicable. A shared helper needs behavioral regression
tests and representative caller coverage.

Database constraints, locking, rollback, concurrent inventory updates and
cross-module workflows require PostgreSQL integration tests. HTTP tests must
check status, envelope, permission checks and safe error content. React tests
must exercise user-visible loading, validation and failure states as well as
successful interactions. Browser checks cover real layout, focus and responsive
behavior; DOM tests alone cannot prove these properties.

Fresh-install and migration tests must also run an unfiltered `alembic check`:
schemas rebuilt from ORM metadata do not prove that deployed migrations match
the models. Keep deployed migration history immutable and reconcile drift through
reviewed forward migrations, including locking, retry and downgrade behavior.

Keep the [local seed entry point](local-seed-data.md) aligned with supported
workflows. Verify it on a newly migrated disposable database and repeat it:
business document counts, ledger entries and stored files must not duplicate.
Use service transitions for stock and money; never fabricate final statuses or
edit a real database to make a fixture pass. A custom company must be selected
explicitly when the same administrator email exists in multiple tenants.

Run from the repository root:

```sh
make check       # Windows: .\make.ps1 check
make test-cov    # Windows: .\make.ps1 test-cov
```

`check` runs contract checks, lint/format/type checks, backend unit/integration/API
tests, frontend tests and the production web build. The build also enforces
bundle budgets and scans bundled assets for credentials. `test-cov` measures
backend statement and branch coverage, including application startup and route
wiring. The completion gate and CI enforce an 80% combined statement/branch
coverage floor for the full backend suite; CI retains `coverage.xml`.
Read uncovered branches and add meaningful tests; a passing suite or a coverage
percentage alone does not prove every important scenario is tested.

Run `make test-e2e` and the populated-screen checks described in
[ui-readiness.md](ui-readiness.md) for affected browser flows. Check mobile,
tablet and desktop widths, keyboard focus, scrolling, long content, validation
and dialogs. A failed API, redirected page, missing fixture or unavailable
service is an unresolved check, never evidence that a screen passed.

CI additionally verifies dependency locks and audits, release configuration,
migrations, container builds/scans and production-stack browser journeys. Local
`check` covers the application checks; these deployment checks remain required
for release. Keep the [pre-commit/pre-push hooks](../.pre-commit-config.yaml)
enabled and fix failures without weakening rules or bypassing checks.

Record the commands, results and any blocked verification when handing work over.
Existing production-readiness findings remain open until their own acceptance
criteria are met; a standards refactor does not close unrelated business or
deployment gaps.

## Repository hygiene

Keep caches, coverage results, browser sessions and generated test reports out of
source control and Docker build contexts. Preview disposable outputs with
`python scripts/clean.py --dry-run`; remove them with `make clean` or
`.\make.ps1 clean`. Both commands share the same helper, which checks workspace
boundaries, refuses links and preserves tracked files, dependencies, credentials,
runtime storage, backups and handbook deliverables. Archive verification reports
outside the checkout before cleaning when they are needed for a handover.

Keep migrations, tests, dependency locks, generated API contracts and referenced
documentation. Move superseded planning checkpoints into the
[implementation archive](implementation/archive/BUILD-ORDER.md) with a historical
label instead of presenting them as current implementation status.
