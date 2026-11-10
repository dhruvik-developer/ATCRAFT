# Migration squash: 38 revisions to one baseline (8 November 2026)

On 8 November 2026 (commit `299d95bb`) the Alembic history was squashed again
to a single baseline revision, `20261127_0100` - the id names the revision, not
the date - so a new checkout and a new database start from one readable
baseline instead of the October baseline plus thirty-seven forward files. The
application schema did not change relative to former head `20261126_0100`.

| | Before | After |
|---|---|---|
| Head | `20261126_0100` | `20261127_0100` |
| Revision files | 38 | 1 |
| Public tables | 156 | 156 |
| Columns | 3,108 | 3,108 |
| Constraints | 3,790 | 3,790 |
| Indexes | 1,194 | 1,194 |
| Triggers | 8 | 8 |
| Functions | 284 | 284 |

The pre-squash chain is preserved at git tag `pre-migration-squash-20261127`
(commit `904df1b2`, an ancestor of `main` and `develop`, so it can be checked out
by hash where the tag has not been fetched). The earlier October squash remains
at `pre-migration-squash-20261019` (commit `af1186b4`).

Revisions written after the squash revise the baseline, so the chain is no
longer a single file: `20261128_0100` (batch SKU conversion lineage) follows it.
The commands below say which output depends on that.

## How the baseline was produced

`20261127_0100_fresh_baseline.py` was generated from `pg_dump --schema-only` of
a scratch database migrated through the old chain to `20261126_0100` — not from
SQLAlchemy metadata. Database-level constraints, defaults, partial indexes,
CHECK vocabularies and append-only triggers that the models do not declare are
preserved. The helper is `apps/api/scripts/build_fresh_baseline.py`.

A fresh old-chain database and a fresh baseline database matched on every
catalog category above.

## Fresh installation

```bash
cd apps/api
alembic upgrade head   # the baseline, then every later revision
alembic current        # 20261128_0100 (head), until a later revision lands
```

## Existing database: verify, then stamp

Existing databases that already sit at former head `20261126_0100` must be
**stamped**, never upgraded through the new baseline.

1. Backup, restore into a clone, confirm restore.
2. Confirm `SELECT version_num FROM alembic_version;` is exactly `20261126_0100`.
   Any earlier revision must first be upgraded through the old chain at tag
   `pre-migration-squash-20261127` (commit `904df1b2`) before stamping.
3. On the clone:

   ```bash
   cd apps/api
   export DATABASE_SYNC_URL='postgresql+psycopg://.../<clone>'
   alembic stamp --purge 20261127_0100
   alembic current                     # 20261127_0100 - no longer head
   alembic upgrade head                # runs only the revisions after the baseline
   alembic current                     # 20261128_0100 (head), or a later one
   ```

4. Confirm catalog counts still match the baseline plus the later revisions;
   smoke-test login and one write.
5. Repeat the stamp on the target in a maintenance window. The normal guarded
   release then applies the later revisions, as it did on the clone.

Never run the baseline upgrade against a populated pre-squash database.

## Local development databases behind head

A local `atcraft_erp` that was not at `20261126_0100` cannot be stamped safely.
Recreate it:

```powershell
.\make.ps1 db-fresh
```

That drops the development database, applies the baseline and every later
revision through head, loads baseline reference data and the full demonstration
stages.

## Next migration

A new revision's `down_revision` is the current head as `alembic heads` prints
it, not the baseline: `20261128_0100` already revises `20261127_0100`, so the
next one names `20261128_0100` unless another has landed since.

```python
down_revision = "20261128_0100"
```

`alembic heads` must print exactly one head after every change.

## Tests

The single application-level proof is
`apps/api/tests/integration/test_migration_baseline.py` (fresh upgrade,
catalog shape, downgrade/rebuild). The baseline's downgrade drops every table
with `CASCADE` and exists for disposable databases only; a populated database is
rolled back by restoring its backup. Per-revision migration tests that targeted
retired files (`20261026_0100` … `20261124_0100`) were removed with those
revisions; their upgrade/downgrade contracts remain recoverable at tag
`pre-migration-squash-20261127` (commit `904df1b2`).
