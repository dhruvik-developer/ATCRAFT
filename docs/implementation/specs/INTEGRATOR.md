# Integrator procedure: merge one round's lanes into `main`

> Historical lane integration procedure. Current development integrates on
> `develop`; follow the [current contributing workflow](../../../README.md#contributing)
> and [release runbook](../../../ONE-BOX-RUNBOOK.md) before merging or deploying.

You work in the **main tree** `C:/Project/Other/ATCRAFT` (branch `main`). The lanes are:

| Lane | Path | Branch |
|---|---|---|
| A | `C:/Project/Other/ATCRAFT-lanes/commercial` | `lane/commercial` |
| B | `C:/Project/Other/ATCRAFT-lanes/operations` | `lane/operations` |
| C | `C:/Project/Other/ATCRAFT-lanes/portals` | `lane/portals` |
| D | `C:/Project/Other/ATCRAFT-lanes/supply` | `lane/supply` |
| E | `C:/Project/Other/ATCRAFT-lanes/quality` | `lane/quality` |

Lane D exists from round 2 and lane E from round 2 (resumed). Wherever this procedure says "A then B then C", continue with D and then E (merge order A, B, C, D, E; re-chain migrations in that order; fast-forward and re-clone `atcraft_erp_lane_d` and `atcraft_erp_lane_e` too). Lanes that had no work in a round have no new commits. Skip their merge, but still fast-forward them.

Read `BRIEF.md` (same folder) for the commands and rules; they apply here with paths in the main tree. The main tree's `.env` points at the real dev DB `atcraft_erp` and test DB `atcraft_erp_test`. Never install or remove dependencies. Never push. Never use `--no-verify`.

## 0. Preconditions

- `git status` in the main tree is clean. Only the untracked `Atcraft Manufacturing ERP Development_compressed.pdf` is expected; never add it.
- Every lane has a clean `git -C <lane> status`. If a lane has uncommitted work, STOP and report: do not commit it yourself.
- Record `OLD_HEAD=$(git rev-parse HEAD)` and the current Alembic head (`apps/api/.venv/Scripts/python.exe -m alembic heads` from `apps/api`).

## 1. Merge

Merge each lane in turn, A then B then C:

```
git merge --no-ff --no-edit lane/commercial
git merge --no-ff --no-edit lane/operations
git merge --no-ff --no-edit lane/portals
git merge --no-ff --no-edit lane/supply
git merge --no-ff --no-edit lane/quality
```

Resolve conflicts properly after each merge before starting the next. Conflicts here are almost always **additive** (both sides appended to the same list, enum, catalogue, router, nav, `STATUS_VARIANTS`, `permissions.ts`, sweep scripts or docs): keep both sides, in a sensible order, with no duplicates.

| Kind of file | How to resolve |
|---|---|
| Generated (`openapi.json`, `apps/web/src/types/openapi.ts`, `postman_collection.json`) | Take either side now (`git checkout --theirs`), then **regenerate** in step 3 |
| `docs/api.md` count line | Fix in step 3 |
| `docs/assumptions.md` | Keep all new sections; renumber in step 2 |
| Same function or class edited by two lanes | Read both intents and write the union carefully. Run that module's tests afterwards |

`git merge --continue` (or commit) after each lane. Never resolve by dropping a lane's work.

## 2. Post-merge reconciliation

### a. Alembic: exactly one linear head

1. Run `alembic heads`.
2. If there are several heads, keep lane A's chain as is.
3. Point the FIRST new migration of lane B at lane A's LAST new revision.
4. Point lane C's first new migration at lane B's last, then lane D's first at lane C's last, then lane E's first at lane D's last.
5. In each re-pointed file, edit both `down_revision` and the docstring `Revises:` line. Revision ids never change.
6. Re-run `alembic heads`; it must show 1 head.

### b. Enum CHECK lists touched by more than one lane

The usual case is `report_snapshots.report_key_is_known`; also check the PO and vendor-invoice status CHECKs if two lanes touched them.

- The LAST migration in the chain that sets that CHECK must list **every** current enum value: compare with `ReportKey` in `apps/api/src/modules/reports/models.py`.
- If it does not, add one small migration at the end of the chain (`*_reconcile_report_keys`) that drops and re-adds the CHECK `NOT VALID` with the full literal list, then runs `VALIDATE`.
- Its `downgrade()` restores the previous literal list. It must first delete rows whose key is not in that list, as the precedent `20260905_0100_report_snapshots_packing_list_key.py` does.

If `apps/api/tests/unit/test_report_key_check_migration.py` does not exist yet, create it:
- Parse `apps/api/migrations/versions/*.py`.
- Find the migrations that create or alter `report_key_is_known`.
- Order them by the revision graph (walk the `down_revision` links).
- Assert that the latest one's literal list equals `{k.value for k in ReportKey}`.

### c. Assumption numbers

Find every placeholder heading `## §<WP>-<n>.` in `docs/assumptions.md`, in order of appearance.

1. Assign the next free integer numbers after the last numbered section.
2. Replace each heading with `## <N>. <title>`.
3. Replace every occurrence of the token `§<WP>-<n>` across the repository with `§<N>`. Cover code comments, docs, the handbook and tests: `git grep -l "§[A-Z][0-9]*-[0-9]"`. Do it with a small script.
4. Make sure the new sections sit before the "Open questions" part and use the house format (Assumed / Why / Where / Impact if wrong, separated by `---`).

### d. Other shared catalogues

Check for duplicates or ordering problems in:
- `PERMISSION_GROUPS`
- `ROLE_PRESETS`
- `lib/permissions.ts`
- `STATUS_VARIANTS`
- `NotificationType` and `notification-types.ts`
- `ReportKey` and `reports-types.ts`
- `DocTypes`
- `MODULE_ORDER`
- `route-loaders` / `router` / `navigation`
- the e2e sweep scripts
- the demo data stage registry

## 3. Regenerate and recount

From `apps/api`, run the contract export: `python -m scripts.export_openapi`, then `npm run contract:types` from `apps/web`, then `python -m scripts.export_postman`.

Then update `docs/api.md`'s `"<N> requests in <F> folders"` line, using `tests/unit/test_postman_and_contract_targets.py` to learn the exact numbers. Add every new path to the endpoint map.

## 4. Verify, and fix integration breakage on `main`

Commit fixes with clear messages.

### Backend
- `ruff check src tests migrations scripts`
- `ruff format --check src tests migrations scripts`
- `mypy src`
- The FULL unit suite: `pytest tests/unit -q -p no:cacheprovider`
- Integration and API tests for every test file the lanes added or changed this round, and for every module whose service file had a merge conflict. Get the list from `git diff --stat OLD_HEAD..HEAD -- apps/api/tests`. Use `-o faulthandler_timeout=900` and run them one command at a time.

### Migrations (scratch DB; never the real dev DB in this step)

1. Create the scratch DB:
   ```
   docker exec atcraft-erp-postgres-1 psql -U atcraft -d postgres -c "DROP DATABASE IF EXISTS atcraft_erp_migcheck"
   docker exec atcraft-erp-postgres-1 psql -U atcraft -d postgres -c "CREATE DATABASE atcraft_erp_migcheck TEMPLATE atcraft_erp"
   ```
   The real dev DB has no connections while nobody is running the API.
2. Run alembic against it by setting `DATABASE_SYNC_URL` in the process env (the env var overrides `.env`). Use the same credentials as `.env` with DB name `atcraft_erp_migcheck`.
3. Run `upgrade head`, then `downgrade <old head recorded in step 0>`, then `upgrade head`. All must succeed.
4. Drop the scratch DB.

### Frontend (`apps/web`)
- `npm run typecheck`
- `npm run lint`
- `npm run format:check`
- `npx vitest run` over every test file changed or added this round, plus `src/app` and `src/lib`: `--no-file-parallelism --maxWorkers=1`
- `npm run build` (bundle budget)

### Hooks over the whole round
Some lane commits may have skipped hooks earlier in the programme. From the repository root, run `pre-commit run --from-ref <OLD_HEAD> --to-ref HEAD`. It must pass. If it rewrites files, commit the result.

### Contract
- `python -m scripts.export_openapi --check`
- `npm run contract:check`
- `python -m scripts.export_postman --check`

Commit the reconciliation and fixes, for example "Integrate round N: ..." with a body listing what was reconciled. Use the commit trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Hooks will run.

## 5. Bring the real dev DB and the lanes forward

1. Migrate and refresh the real dev DB. From `apps/api` in the main tree, run `python -m alembic upgrade head`, then `python -m src.core.seed`, then `python -m src.core.demo_data`. Run demo_data twice; the second run must create nothing new or fail nothing.
2. Fast-forward every lane: `git -C <lane> merge --ff-only main`. All three must succeed.
3. Re-clone every lane DB from the migrated dev DB, so lane DBs never carry a chain that was later re-pointed. For x in a, b, c, d, e:
   ```
   DROP DATABASE IF EXISTS atcraft_erp_lane_x WITH (FORCE)
   CREATE DATABASE atcraft_erp_lane_x TEMPLATE atcraft_erp
   ```
   Then confirm with `python -m alembic current` run from one lane: it must print the new head.
4. Run it (the user's standing rule). From the main tree start the host API (`apps/api/.venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8000`) and Vite (`npm run dev -- --host 127.0.0.1 --port 5173 --strictPort` in `apps/web`), both in the background. Check `curl http://127.0.0.1:8000/health/ready`. Then from `tests/e2e` run `npx playwright test`, `node scripts/browser-sweep.mjs` and `node scripts/responsive-sweep.mjs`. Every journey must pass. The browser sweep must report `broken=0 http-failures=0`; it navigates client-side, so an HTTP failure there is real. Investigate any responsive-sweep flag on a screen this round touched, and fix it if this round caused it. Stop both servers afterwards.

## 6. Return

- merge commits and fix commits;
- conflicts resolved, per file, with how each was resolved;
- the migration re-pointing done;
- CHECK reconciliation;
- assumption renumbering map;
- every verification command with exact results (pass and fail counts);
- the dev-DB migrate/seed/demo results;
- lane fast-forward results;
- anything left broken, stated plainly.

**Never claim a check passed unless you saw it pass.** If something cannot be fixed safely, leave `main` building where possible, describe the failure precisely, and let the orchestrator decide.
