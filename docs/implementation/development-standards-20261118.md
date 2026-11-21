# Application standards pass — 8–22 November 2026

This pass applied the shared [development standards](../development-standards.md)
across the existing FastAPI and React application, after the
[22–23 October verification](development-standards-20261022.md). Existing business
behaviour was kept: each change preserves valid inputs and existing callers, and
every refusal that tightened a write was checked against the screens that send
it. A green gate does not certify every function as fully tested; the open items
are listed at the end.

## How it ran

Two read-only audits (8 November at `2b08487`, 10 November at `31cbb544`) produced
findings that an independent verifier confirmed before any fix. Fix groups for
the backend, the frontend and the documents then worked through them on
`develop`, each verifying its items against the current code first, adding the
tests with the fix and committing in small commits. 38 groups returned a
structured result: 274 items fixed and 89 deliberately not changed, each with a
recorded reason (already fixed, intended and documented, or a contract change
larger than the defect). Three later groups were paused after committing their
items; their last in-progress edits were finished in `713cb953`. A fourth, on
test-support duplication, did not start and is listed under the open items.

## What changed, by standard

1. **API contracts.** Request bodies refuse unknown keys; numeric and text bounds
   fit their columns; every route is permission-gated or allowlisted with a
   reason; empty pages answer the standard envelope; `docs/api.md`'s error-code
   table is checked against `ErrorCode`.
2. **Responsive design system.** Shared primitives replaced hand-built copies
   (`components/ui/table.tsx`, `multi-select-list.tsx`, `search-picker.tsx`,
   `components/chart.tsx`, `default-badge.tsx`, `field-error.tsx`,
   `lifecycle-stepper.tsx`); scanners refuse the layout traps CLAUDE.md records,
   small touch targets, raw buttons and unlabelled tables.
3. **Messages.** Sentences raised from two or more modules live in
   `src/core/messages.py`; the frontend's shared headlines, plurals and success
   builders live in `lib/messages.ts`, and refusals are worded as the action a
   person takes.
4. **Exceptions and logging.** One traceback per failure, the application's
   logging configured for every test session, database error text never shown,
   and no Celery result kept (so a failure's exception is never stored).
5. **Tests.** 95 new backend unit and guard test files and 22 shared test
   support modules (`tests/*_support.py`), plus screen tests beside each
   frontend fix.
6. **Comments and documents.** Every source file has a module docstring and every
   long branching function states its rule (`test_source_docstrings.py`);
   ASSUMPTION comments cite their `docs/assumptions.md` sections where one exists
   (`test_assumption_references.py`).
7. **Naming.** A module's own repository is named for what it reads, never with
   another module's owner name (`test_repository_names.py`); lock reads and
   state machines follow one vocabulary.
8. **Clean, modular code.** Imports follow `docs/architecture.md`'s direction
   (`test_module_dependency_direction.py`); copies of repository classes, shared
   helpers and dead definitions are refused (`test_repository_copies.py`,
   `test_shared_helper_definitions.py`, `test_dead_definitions.py`).
9. **Validation and security.** Tenant references are read through
   tenant-scoped repositories rather than trusted as foreign keys; screens gate
   on the permission their endpoint enforces; dashboard tiles and saved report
   files require their domain permissions (see the recheck below).
10. **Performance and reliability.** N+1 loops were batched and pinned with
    statement counters (`tests/statement_support.py`); workers never page with
    OFFSET; reservations lock in one order.
11. **Shared utilities.** `lib/decimal-input.ts`, `lib/form-values.ts`,
    `lib/csv.ts`, `lib/storage.ts`, `hooks/use-form-seed.ts` and one URL filter
    updater in `use-list-query`; on the backend `paginate_rows`, `labels_by_id`,
    `apply_transition` and the warehouse owner's `WarehouseLookupRepository`.
12. **Continuous checks.** The guards above run in the pre-push hook and CI;
    ESLint's named rules are proven in `apps/web/src/test/lint-rules.test.ts`.

## Recheck, 17–18 November

An independent review read the last commits again before release:

- `713cb953` (dashboard and saved-report permissions, the shared
  `apply_transition`, the repaired dispatch test, the dashboard trends split and
  the production option hooks) was confirmed correct. The review found an older
  gap the new report-file policy left open: trial-balance and GSTR-1 files were
  readable with `report.view` alone, without `ledger.view` or
  `gst_report.view`. It is closed and tested in `730d88a6`. Filing a document
  against a report job now also requires the job's own access (`1028a037`), so
  an upload no longer reveals whether a job the filer cannot open exists.
- The operations backend (43 commits) and the commercial and finance backend
  (44 commits) were reviewed hunk by hunk: no regression in filters, tenant or
  warehouse scope, locks, ordering, money rounding, approvals, status changes or
  audit rows.

## Reviews and fixes, 19–22 November

- **Eager loading (`9da0056c`).** After sign-in every screen and form downloads
  in the background. A failed background download never reloads the page
  (`lib/stale-bundle.ts`, `lib/preload.ts`); only a download the person is
  waiting on does.
- **Recheck rounds (19–20 November).** Four rounds of review and fix closed the
  open items of the first pass. Every `ASSUMPTION` paragraph now cites a recorded
  section, and `tests/unit/test_assumption_references.py` refuses one that does
  not. Warehouse checks share `require_active_warehouses` and
  `require_warehouse_in_scope` in `warehouses.service`. The second
  `exists_in_tenant` is gone. `report_snapshots.document_id` has a partial index
  (migration `20261129_0100`). Integration tests build their schemas through
  `tests/private_schema_support.py`. The same rounds also brought:
  - edge rate limits sized for a plant behind one address;
  - MRP no longer planning an item twice;
  - a nightly purge of lapsed documents;
  - an audit row for every demoted default.
- **Final independent review (20 November).** The fix commits were reviewed in
  five slices, and an adversarial verifier checked every finding: 16 were
  confirmed. The owner decided two of them on 20 November:
  - document lines use the item's stock unit only, until documents convert units
    (`docs/assumptions.md` §UOM-1);
  - job-work output that an inspection rejects stays charged, and the bill is
    adjusted by hand (§JW-4).

  All 16, and the follow-ups the reviews collected, were fixed between
  `7639b88e` and `6875b519`.
- **Last review (22 November).** Two reviewers read those commits again and
  confirmed six gaps, fixed in `e8eda19b` and `ccd3cf3a`:
  - a line saved in another unit before the rule gets a refusal the person can
    act on, and is never relabelled to the stock unit;
  - a sales order with such an open line ships nothing until it is raised again
    in the stock unit;
  - MRP plans nothing from a packing recipe whose material is lot- or
    unit-tracked, and tracking cannot be turned on for a recipe's material.

  `ONE-BOX-RUNBOOK.md` has a one-off check to run before deploying the
  stock-unit release.

## Verification

CI ran green on `713cb953` (backend quality with dependency audit, backend
integration, frontend with bundle budgets, the production-stack e2e journeys and
release configuration). In the 19–22 November batches:

- each lane ran the unit and integration tests of the modules it changed;
- every commit passed the pre-commit hooks;
- the production web build met every bundle budget at `6875b519` (first load
  201.5 of 210 KB).

The pre-push hook and CI gate every later commit.

## Open items

- `SQL_OUTSIDE_REPOSITORIES` in `tests/unit/test_layering_guards.py` still lists
  the files that build statements outside a repository; the guard refuses any new
  one.
- Quality still announces an inspection once per line (`BE4-06`): batching it
  changes the "N times" badge, a product decision.
- Documents do not convert units yet (§UOM-1); conversion at posting is a
  separate change. A sales order already partly dispatched with an open line
  saved in another unit cannot be completed. Closing it needs a new feature,
  which is an owner decision; the pre-release check lists such orders.
- Owner decisions on job work: reducing the vendor charge automatically when an
  inspection rejects output (§JW-4). Stock at a job-work vendor carries no book
  value, and an unprocessed return comes back at standard cost (§JW-5).
- Owner decisions carried over: the two documented PATCH-null conventions
  (`docs/api.md`), the notes length bound, unscaled quantities in allocation and
  inventory, and the business-date policy (the server's "today" is UTC while
  screens use the local date).
