# ATCRAFT gap build: brief for every implementation and review agent

> Historical lane workflow. The work-package requirements remain acceptance
> references; branch, worktree and integration instructions below describe the
> original build. Use the current [contributing workflow](../../../README.md#contributing)
> and [development standards](../../development-standards.md) for new work.
> Do not recreate the lane worktrees or lane databases: the repository has one
> working copy (see [AGENTS.md](../../../AGENTS.md)).

You are one of several agents closing the gaps between the ATCRAFT manufacturing ERP and the
client proposal. Work proceeds in **rounds**. In each round, several **lanes** (git worktrees)
build in parallel. After the round, an integrator merges them into `main`.

The specs, reports and audit live in the main tree under `C:/Project/Other/ATCRAFT/docs/implementation/`. Always read them from
that absolute path; your lane may hold an older copy, or none.

Read this brief fully, then your work-package spec. The CLAUDE.md you were given is binding.
It is the main tree's copy, and your lane holds the identical file.

Background research lives in `C:/Project/Other/ATCRAFT/docs/implementation/reports/`:

| File | Area |
|---|---|
| `anatomy.md` | How to add a feature end to end, and every meta-test that polices new code |
| `sales-finance.md` | Sales, finance, GST, PDFs, job-work cost |
| `returns-inventory-qc.md` | Customer returns: inventory, dispatch and QC seams |
| `procurement-vendor-portal.md` | Requisition to PO, the vendor portal, vendor-scope security holes |
| `notifications-alerts.md` | Notifications, Web Push design, smart-alert engine design |
| `masters-pricing.md` | Brand, manufacturer, cost centre, dealer pricing |
| `production-shifts.md` | Shift master and roster design |
| `dealers-reports-docs.md` | Dealer portal design, reports catalogue, documents, collections |

These reports carry exact file and line references and a "Traps" section for each area.
Read the sections your spec names. **Where a spec and a report disagree, the spec wins.**

---

## 1. Where you work: lane isolation (critical)

| Lane | Path | Branch | Dev DB | Test DB |
|---|---|---|---|---|
| A, commercial | `C:/Project/Other/ATCRAFT-lanes/commercial` | `lane/commercial` | `atcraft_erp_lane_a` | `atcraft_erp_test_a` |
| B, operations | `C:/Project/Other/ATCRAFT-lanes/operations` | `lane/operations` | `atcraft_erp_lane_b` | `atcraft_erp_test_b` |
| C, portals | `C:/Project/Other/ATCRAFT-lanes/portals` | `lane/portals` | `atcraft_erp_lane_c` | `atcraft_erp_test_c` |
| D, supply | `C:/Project/Other/ATCRAFT-lanes/supply` | `lane/supply` | `atcraft_erp_lane_d` | `atcraft_erp_test_d` |
| E, quality | `C:/Project/Other/ATCRAFT-lanes/quality` | `lane/quality` | `atcraft_erp_lane_e` | `atcraft_erp_test_e` |

Lane names are historical. Any lane may be assigned any work package; follow the spec, not the lane name. The "Lane X, round N" line at the top of a spec may be out of date: **your prompt names your real lane root and step, and that is authoritative.** `PLAN.md` holds the current schedule.

**Traceability.** The client wants *every* item in the proposal PDF present. Many specs cite checklist IDs (for example `K08`, `P05.S8`). For each ID you are assigned, read its full audit entry (evidence, exact gap, recommendation) in `C:/Project/Other/ATCRAFT/docs/implementation/trace/trace.json` (search for `"id": "K08"`). Read the cross-cutting defect notes in `C:/Project/Other/ATCRAFT/docs/implementation/trace/notes.md` too. Your package is done only when each assigned ID is genuinely closed: backend, a usable screen, tests and docs. A recommendation in the audit is guidance; where your spec decides differently, the spec wins.

- **Edit only files under YOUR lane root.** Use absolute paths under it for every Read, Edit, Write and Grep.
  - Never edit `C:/Project/Other/ATCRAFT` (the main tree, branch `main`) or another lane.
  - Your harness's default working directory is the main tree, so be deliberate: `cd` into your lane in every shell command.
- **Your lane `.env` is already wired.**
  - `DATABASE_URL` and `DATABASE_SYNC_URL` point at your lane's own dev DB. It is a clone of the real dev DB, with baseline and demo data, migrated to your lane's head.
  - Leave `TEST_POSTGRES_DB` unset; use the standard `atcraft_erp_test` database.
  - Postgres runs at 127.0.0.1:5433 and Redis at 127.0.0.1:6380 (Docker, already up).
- **Shared environment: do not touch.** `apps/api/.venv`, `apps/web/node_modules` and `tests/e2e/node_modules` are junctions to the main tree's copies. **Never** run any of these, because they would destroy the environment every lane shares:
  - `pip install`, `pip uninstall`
  - `npm install`, `npm ci`, `npm uninstall`, `npm update`
  - deleting `.venv` or `node_modules`, or `make clean`
- **No new dependencies.** Nothing in this programme needs one. If you are convinced one is needed, stop and report.
- **Git rules.**
  - Commit only to your lane branch.
  - Never push, merge, rebase, reset or amend, and never touch other branches.
  - Never use `--no-verify`.
- Local greenlet is pinned to 3.2.4 because Windows Smart App Control blocks 3.5.5. Leave it alone.
- **Hooks and Smart App Control.** Smart App Control also refused some pre-commit console launchers (`end-of-file-fixer.exe`, `check-json.exe`). That is fixed: `.pre-commit-config.yaml` runs every pre-commit-hooks hook as `python -m pre_commit_hooks.<module>` (main `44647f9`, also committed in every lane). Do **not** use `SKIP=` or `--no-verify`. If a tool fails with "An Application Control policy has blocked this file" (WinError 4551), stop and report it, naming the blocked file. Do not work around it.
- **Exporter line endings.** On Windows the OpenAPI/Postman exporters write CRLF. The `mixed-line-ending` hook converts to LF, which is fine. Re-stage and commit again if the hook rewrote files.

## 1a. Taking over interrupted work

Usage limits have stopped agents mid-package more than once. Codex resumed round 2 after a Claude limit and was then itself stopped, leaving changes uncommitted in lanes A, B and D. When your step says the package was started by someone else, take it over this way:

1. **Inventory before editing.** Read the spec, then every uncommitted change:
   - `git status`, `git diff`, and each untracked file.
   - Map each change to the spec's numbered decisions and test list: done, partial, missing or wrong.
   - The previous agent's notes, if any, are a claim to check, not a fact.
2. **Keep what is right.** Never discard the previous agent's work wholesale (`git checkout -- .`, `git stash`, `git clean`, deleting files you did not inspect). Fix what is wrong, finish what is missing.
3. **Scratch is not a deliverable.** Untracked helpers such as `tmp/`, `*.tmp.mjs`, `browser-sweep.json`, `*.log` and ad-hoc scripts must not be committed. Delete each one once you have confirmed it is scratch.
4. **Line endings.** Files the previous agent wrote may be CRLF; git normalises them to LF on add. If a diff looks like a whole-file rewrite, compare with `git diff --ignore-cr-at-eol` before judging it.
5. **Prove it yourself.** Re-run every check the brief requires on the final tree. Nothing the previous agent reported counts as verification.
6. Commit in logical commits, one package per commit where the changes separate cleanly.

## 2. Commands

**Backend.** Run from `<LANE>/apps/api`, with `PY=<LANE>/apps/api/.venv/Scripts/python.exe`.

| Purpose | Command |
|---|---|
| One unit file | `$PY -m pytest tests/unit/test_x.py -q -p no:cacheprovider` |
| Whole unit suite (several minutes; run once before your final commit) | `$PY -m pytest tests/unit -q -p no:cacheprovider` |
| Integration | `$PY -m pytest tests/integration/test_x.py tests/api/test_y.py -q -p no:cacheprovider -o faulthandler_timeout=900`. Run only your feature's files and their direct neighbours. The whole suite takes over an hour. |
| Lint | `$PY -m ruff check src tests migrations scripts` and `$PY -m ruff format src tests migrations scripts` |
| Types | `$PY -m mypy src` (a few minutes; must pass) |
| Migrations | `$PY -m alembic heads` (must print exactly ONE head), `$PY -m alembic upgrade head`, `$PY -m alembic downgrade -1`, `$PY -m alembic current` |
| Baseline and demo data on your lane DB | `$PY -m src.core.seed` and `$PY -m src.core.demo_data` (idempotent; run demo_data twice to prove it) |
| Contract, after ANY API change, in this order | `$PY -m scripts.export_openapi`, then `npm run contract:types` from `apps/web`, then `$PY -m scripts.export_postman` from `apps/api` |

After regenerating the contract, update the `"<N> requests in <F> folders"` sentence in `docs/api.md`. `tests/unit/test_postman_and_contract_targets.py` states the expected numbers when it fails.

**Frontend.** Run from `<LANE>/apps/web`.

| Purpose | Command |
|---|---|
| Types | `npm run typecheck` |
| Lint | `npm run lint` |
| Format | `npm run format`, then `npm run format:check` |
| Tests | `npx vitest run <paths...> --no-file-parallelism --maxWorkers=1`. Run only the files you added or changed, plus close neighbours such as `src/app` when you touch routing. The full suite takes about 15 minutes; do not run it. |
| Build (once, at the end of frontend work) | `npm run build`, which runs `tsc -b`, the Vite build and the **bundle budget**: each route chunk ≤ 20 KB gzipped, first load ≤ 210 KB. Split big dialogs into their own lazy chunks if a budget fails. |

The machine has 4 cores and up to five lanes run at once. Be economical: run targeted tests, never whole suites except the unit suite once. Use timeouts on long commands.

**Live check in your lane (frontend and review steps of any package with screens).** Green unit suites have hidden real runtime defects here, such as a list endpoint that answered 500 the first time it held a row. So look at your screens running:

1. Start the lane API in the background: from `<LANE>/apps/api`, run `$PY -m uvicorn src.main:app --host 127.0.0.1 --port <API_PORT>`. It reads your lane `.env`, so it uses your lane DB.
2. Start Vite: from `<LANE>/apps/web`, set the environment variable `VITE_API_PROXY_TARGET=http://127.0.0.1:<API_PORT>`, then run `npx vite --host 127.0.0.1 --port <WEB_PORT> --strictPort`.
3. Ports by lane:

   | Lane | `API_PORT` | `WEB_PORT` |
   |---|---|---|
   | A | 8710 | 4710 |
   | B | 8720 | 4720 |
   | C | 8730 | 4730 |
   | D | 8740 | 4740 |
   | E | 8750 | 4750 |

   Never use 8000 or 5173; they belong to the main tree. Never start Docker.
4. From `<LANE>/tests/e2e`, with `E2E_BASE_URL=http://127.0.0.1:<WEB_PORT>` and `E2E_API_URL=http://127.0.0.1:<API_PORT>/api/v1` set, run:
   - `node scripts/browser-sweep.mjs`, which must end `broken=0 http-failures=0`;
   - a short Playwright script of your own that opens your new screens and dialogs, does one real create through the UI, and lists what it created;
   - `npx playwright test` when you touched a flow a journey covers.

   The lane DB is a clone of the dev DB, so the admin credentials in `tests/e2e/.env` apply.
5. **Stop both servers before you finish.** Kill the processes you started, and only those.

## 3. Non-negotiables for every package (in addition to CLAUDE.md)

1. **Layering, tenant scope, audit, state machines, Decimal money, pagination, no N+1, and `lazy="raise"`.** CLAUDE.md lists these, and the reports show how each is done in this codebase.
2. **Traps.** Read CLAUDE.md's trap list and your report's "Traps" section. Those defects already shipped once.
3. **New permission.** Each one needs all of:
   - the `Permission` enum member and a `PERMISSION_GROUPS` entry;
   - the presets in `src/core/permissions.py`;
   - `apps/web/src/lib/permissions.ts`;
   - a grant migration for system roles, modelled on `20260906_0902_grant_item_attribute_permissions.py`;
   - `require_permission(Permission.X)` written **literally** in the route decorator, because the gate test parses it;
   - the UI gate `P.X` inside the calling feature folder.
4. **New `*Status` or `*State` StrEnum value** → add it to `STATUS_VARIANTS` in `apps/web/src/components/shared.tsx`.
5. **New static page route** → all of these:
   - `app/route-loaders.ts`
   - `app/router.tsx` `routeGroups`
   - `app/navigation.ts`, if the page sits in the sidebar
   - the four sweep scripts `tests/e2e/scripts/{browser-sweep,a11y-sweep,responsive-sweep,permission-sweep}.mjs`
   - `tests/e2e/scripts/capture-screens.mjs` and `docs/handbook/content.json`, when the page is user-facing
6. **New setting** → `src/core/config.py`, `.env.example` (`test_env_template.py`), and the `x-api-env` block of `docker-compose.yml`. If the setting is a secret, also `deploy/*.env.example` and `scripts/deploy.sh`.
7. **Messages and constants.** Backend sentences raised from two or more modules go in `src/core/messages.py`. On the frontend, shared headlines and success builders go in `lib/messages.ts`, and page sizes, debounce values and formats go in `lib/constants.ts` (no literal `limit: 20/200` or `300` ms anywhere, tests included).
8. **Frontend data and screens.**
   - Use TanStack Query, with keys from `queryKeys` or a feature key factory under a shared prefix. Lists use `useListQuery` + `DataTable`.
   - Types come from `SchemaOf<'Name'>`. Any hand-written mirror must be registered in `tests/unit/test_frontend_contract_types.py`.
   - Never mark a column sortable unless the repository's `sortable_fields` allows it.
   - Every screen must be responsive at phone, tablet and desktop widths. Watch the `shrink-0` + `flex-wrap` trap, and make dialogs scroll inside `DialogContent`.
   - The shared `Select` (`components/ui/select.tsx`, since round 1) **drops `onValueChange('')`**: Radix sends `''` when a form resets to a saved value before its options load, and that used to clear the field. An optional select therefore offers "none" as the `SELECT_NONE` sentinel from `lib/constants.ts` and maps it to `null` (see `features/admin/cost-centre-select.tsx`). Never design a select whose "none" is the empty string.
   - Typed test fixtures must carry every required field of the generated type, e.g. `PurchaseOrderSummary.item_category_id` (use `null`).
   - **Bundle budgets are tight.** After round 1 the shared-components chunk is at 33.4 of its 36 KB gz budget and first load at 194 of 210 KB. A component used by one feature lives in that feature's folder, so it lands in the lazy route chunk. Promote to `components/` only what three or more features share. Never raise a budget to make a build pass; split or lazy-load instead.
9. **Migrations.**
   - Hand-written or autogenerated-then-cleaned. Autogenerate renders `GUID()`, `StatusEnum()` and `CIText()`; replace them with `sa.Uuid()` / `sa.String(n)`.
   - Include the review docstring: locking, data migration, rollback, size, indexes.
   - Set `down_revision` to your lane's current single head (`alembic heads`).
   - Run `upgrade head`, then `downgrade -1`, then `upgrade head` on your lane DB, and fix anything that fails.
   - FKs on big tables: add them `NOT VALID`, then `VALIDATE`. Hand-written CHECK names must be ≤ 63 bytes once `ck_<table>_` is prepended.
   - A new member of an enum used in a SQL CHECK needs a hand-written drop / re-add `NOT VALID` / `VALIDATE` migration with **literal** values. This includes `ReportKey`, which needs `report_key_is_known` widened: list **every** current `ReportKey` value literally, including yours. The integrator reconciles lists across lanes.
10. **Assumptions.** Mark each business-rule decision `# ASSUMPTION: ...` in code. Add a section at the END of `docs/assumptions.md`, **before** the "Open questions" part if there is one, using a **placeholder number** `§<WP>-<n>`:
    - heading form: `## §A2-1. Invoice numbering fits the 16-character GST limit`
    - code references use the same token: `docs/assumptions.md §A2-1`
    - keep the section format **Assumed / Why / Where / Impact if wrong**
    - the integrator assigns final numbers.
11. **Docs.** Update all of these as they apply:
    - `docs/database.md`: §2 table map, §7 state machines, §8 integration events, plus a subsection when the rules matter
    - `docs/api.md`: count and endpoint map
    - `docs/architecture.md`: modules and dependency direction, when you add a module
    - `docs/security.md`: auth, scope, public surfaces
    - `docs/handbook/content.json`: user-facing screens
    - `README.md`: only if commands or layout change
12. **Demo data.** Give your feature realistic demo rows in the right `src/core/demo_data/<stage>.py`. Make them idempotent by code or identity, never by date.
13. **Tests, written with the feature.**
    - unit: pure rules, `machine.validate()` lifecycle tests, schema validation
    - integration: real Postgres, module-scoped schema pattern, ORM builders from `tests/seed_support.py`, a list test that lists a row you created
    - API: permission and scope, incl. vendor or dealer isolation where relevant
    - frontend: the `renderScreen` harness from `src/test/harness.tsx`, gating with and without the right, exact request bodies
    - fix any meta-test you trip properly; never weaken it
14. **Generated files.** Never edit `openapi.json`, `apps/web/src/types/openapi.ts` or `postman_collection.json` by hand; regenerate them.
    - Pydantic class names become OpenAPI component names, which are **global**. Before adding a schema class, `git grep "class <Name>("` across `apps/api/src`; two modules defining the same name get renamed by FastAPI and silently break every `SchemaOf<'Name'>` on the frontend. Prefix with the module's noun when in doubt (`SalesInvoiceRead`, not `InvoiceRead`).
    - The handbook's Reports chapter states how many reports exist ("Twenty reports exist" after round 1). A package that adds a catalogue report updates that sentence.
15. **Placement.** New backend code must respect the downward dependency direction in `docs/architecture.md`. Lower modules never import higher ones; use the existing port/seam patterns.

## 4. Committing (once your part is verified)

1. Run `git -C <LANE> status` and `git -C <LANE> diff --stat`. Confirm that only intended files changed and that no stray scratch files are present.
2. Run `git -C <LANE> add <paths>`. Add explicit paths or directories you own; never add `.env`, `storage/`, `dist/` or caches.
3. Commit:
   ```
   git -C <LANE> commit -m "<imperative subject, <=72 chars>" -m "<body: what and why, key decisions, tests run>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
   ```
   The pre-commit hooks (ruff, prettier, eslint, hygiene) run here. If they rewrite files or fail, fix the cause, re-add and commit again. Several commits are fine.
4. Finish with a clean tree (`git -C <LANE> status` clean, apart from ignored files).

## 5. What to return (your final message is data for the orchestrator)

Return a concise structured report:
- **status:** `complete`, or `partial` with the reason.
- **commits:** `sha` and subject for each.
- **migrations:** revision id, `down_revision` and purpose for each new file.
- **permissions_added**, **settings_added**, **report_keys_added**, **doc_types_added**, **notification_types_added**.
- **tests:** each command run and its pass/fail counts. Be exact; never claim a pass you did not see.
- **assumption placeholders added**, e.g. `§A2-1 ... §A2-4`.
- **known gaps or follow-ups**, honestly stated.
- **integrator notes:** shared files you touched where merge conflicts are likely, and anything the next round depends on.
