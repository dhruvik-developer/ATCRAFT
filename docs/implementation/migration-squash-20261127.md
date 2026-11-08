# Migration squash: 38 revisions to one baseline (2026-11-27)

The Alembic history was squashed again to a single revision, `20261127_0100`,
so a new checkout and a new database start from one readable baseline instead
of the October baseline plus thirty-seven forward files. The application schema
did not change relative to former head `20261126_0100`.

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

The pre-squash chain is preserved at git tag `pre-migration-squash-20261127`.
The earlier October squash remains at `pre-migration-squash-20261019`.

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
alembic upgrade head
alembic current        # 20261127_0100 (head)
```

## Existing database: verify, then stamp

Existing databases that already sit at former head `20261126_0100` must be
**stamped**, never upgraded through the new baseline.

1. Backup, restore into a clone, confirm restore.
2. Confirm `SELECT version_num FROM alembic_version;` is exactly `20261126_0100`.
   Any earlier revision must first be upgraded through the old chain at tag
   `pre-migration-squash-20261127` (worktree at the tag) before stamping.
3. On the clone:

   ```bash
   cd apps/api
   export DATABASE_SYNC_URL='postgresql+psycopg://.../<clone>'
   alembic stamp --purge 20261127_0100
   alembic current                     # 20261127_0100 (head)
   alembic upgrade head                # must run no migration
   ```

4. Confirm catalog counts still match; smoke-test login and one write.
5. Repeat the stamp on the target in a maintenance window.

Never run the baseline upgrade against a populated pre-squash database.

## Local development databases behind head

A local `atcraft_erp` that was not at `20261126_0100` cannot be stamped safely.
Recreate it:

```powershell
.\make.ps1 db-fresh
```

That drops the development database, applies `20261127_0100`, loads baseline
reference data and the full demonstration stages.

## Next migration

```python
down_revision = "20261127_0100"
```

`alembic heads` must print exactly one head after every change.

## Tests

The single application-level proof is
`apps/api/tests/integration/test_migration_baseline.py` (fresh upgrade,
catalog shape, downgrade/rebuild). Per-revision migration tests that targeted
retired files (`20261026_0100` … `20261124_0100`) were removed with those
revisions; their upgrade/downgrade contracts remain recoverable at tag
`pre-migration-squash-20261127`.
