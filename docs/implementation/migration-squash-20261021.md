# Migration squash: 58 revisions to one baseline (2026-10-19)

The Alembic history was squashed to a single revision, `20261021_0100`, so a
new checkout and a new database start from one readable baseline instead of 58
incremental files. The application schema did not change.

> **Update — 22 October 2026.** The chain now extends through
> `20261027_0100`. That forward migration reconciles the historical missing
> timestamp defaults/indexes, tenant foreign-key deletion policy and equivalent
> CHECK names; the baseline remains unchanged. Fresh migrations must agree with
> the models under an unfiltered `alembic check`. The counts and stamping
> procedure below record the original squash, not the current forward head.
> See [the standards verification](development-standards-20261022.md).

| | Before | After |
|---|---|---|
| Head | `20261020_0100` | `20261021_0100` |
| Revision files | 58 | 1 |
| Extensions | pgcrypto, pg_trgm, btree_gist, pg_stat_statements | same |
| Public tables | 134 application tables | same |
| Constraints / indexes | 3,392 / 1,054 | same |
| Triggers / functions | 4 / 284 | same |

The pre-squash chain is preserved at git tag
`pre-migration-squash-20261019` (commit `af1186b`). Nothing was deleted from
repository history.

## How the baseline was produced

`20261021_0100_fresh_baseline.py` was generated from
`pg_dump --schema-only` of a scratch database migrated through the old chain to
`20261020_0100` — not from SQLAlchemy metadata. That matters because the
migrations contain database-level constraints, defaults, partial indexes,
CHECK vocabularies and append-only triggers that the models do not declare.
Generating from metadata would have silently dropped them.

The only omitted statements were Alembic's own `alembic_version` table and the
psql/SET preamble. Both application extensions and the
`atcraft_reject_row_mutation()` trigger function are included.

## Verification evidence

Performed on 2026-10-19 against the Docker PostgreSQL instance:

1. **Old chain vs baseline, fresh databases.** A scratch database migrated
   through all 58 revisions and a second scratch database built from the
   baseline alone were compared through `pg_constraint`, `pg_index`,
   `pg_trigger`, `pg_proc` and `information_schema`:

   | Category | Old chain | Baseline | Result |
   |---|---|---|---|
   | Tables | 134 | 134 | equal |
   | Columns | 2,771 | 2,771 | equal |
   | Constraints (normalized CHECKs) | 3,392 | 3,392 | equal |
   | Indexes | 1,054 | 1,054 | equal |
   | Triggers | 4 | 4 | equal |
   | Functions | 284 | 284 | equal |

   Raw `pg_dump` text differed on 61 `CREATE TABLE` statements, only in how
   PostgreSQL re-prints casts inside `CHECK ... = ANY (ARRAY[...])`
   (`(ARRAY[...])::text[]` versus per-element `::text`). The catalog comparison
   normalizes casts and confirms the constraint definitions are equal.

2. **Existing dev database.** A `createdb -T atcraft_erp` clone and the live
   dev database both matched the baseline in every category above. Stamping the
   clone and the dev database changed only `alembic_version`; row counts before
   and after were identical (`companies/users/items/sales_orders` =
   `2/39/75/19`). `alembic upgrade head` after stamping prints no upgrade step.

3. **Application-level migration test.**
   `tests/integration/test_migration_baseline.py` starts from an empty database,
   runs `alembic upgrade head`, and asserts the baseline against the models and
   the constraint vocabulary: purchase-return debit allocations and the
   debit-note report key; inventory document columns, the cost-layer pool CHECK
   and item location foreign key; MRP late-supply columns, all nine late-date
   CHECKs and their admission/rejection cases; shift/roster tables, exclusion
   constraints, work-order and production-entry shift foreign keys; the
   report-key CHECK vocabulary used by these flows; all four extensions; the four
   immutability triggers and the guard function. It then runs `downgrade base` and
   `upgrade head` again. It passes.

   The five old per-revision migration test files were consolidated into this
   test because their parent revisions no longer exist. The four tests that
   exercised the data-only FIFO opening-layer correction were removed with that
   revision: the correction no longer runs, and its SQL remains in the
   `pre-migration-squash-20261019` tag for reference. No test is skipped.

4. **Raw round trip.** On the disposable baseline database, `downgrade base`
   leaves only `alembic_version` and the two `pg_stat_statements` views;
   `upgrade head` restores all 137 objects.

## Fresh installation

```bash
cd apps/api
alembic upgrade head
alembic current        # 20261021_0100 (head)
```

## Existing database: verify, then stamp

Existing databases already contain the effects of every historical backfill,
permission grant and CHECK swap. Replaying the baseline would either fail on
dropped source columns or duplicate work, so the baseline must be **stamped**,
never upgraded through.

`alembic stamp 20261021_0100` fails with
`Can't locate revision identified by '20261020_0100'` because that revision no
longer exists in the script directory. Use `--purge`; it clears only the
`alembic_version` row before writing the new revision.

### Procedure

1. Enter the deployment maintenance window and stop application writes.
2. Take a verified backup:
   - `pg_dump -Fc -d <database> -f pre-squash-<date>.dump`
   - Restore it into a scratch database and confirm the restore succeeds before
     touching the target.
3. Confirm the target is exactly at the old head before changing anything:

   ```sql
   SELECT version_num FROM alembic_version;  -- must be 20261020_0100
   ```

   If it is any earlier revision, **stop**: the database is missing historical
   changes. Upgrade it through the old chain at the tag first, or restore the
   correct backup; do not stamp it to the baseline.

4. On a clone of the target (same PostgreSQL major version), run:

   ```bash
   cd apps/api
   export DATABASE_SYNC_URL='postgresql+psycopg://.../<clone>'
   alembic stamp --purge 20261021_0100
   alembic current                     # 20261021_0100 (head)
   alembic upgrade head                # must run no migration
   ```

5. Confirm the clone's schema still matches the baseline (same catalog counts
   and definitions as the evidence table above) and run application smoke
   checks: login, one list screen, one report, and one write that exercises a
   trigger-protected table.

6. Repeat exactly that stamp on the target, then start the new release. Do not
   run `alembic upgrade` against a populated pre-squash database before
   stamping.

7. Record the backup path, old revision `20261020_0100`, and new revision
   `20261021_0100` in the deployment record.

## Rollback and recovery

- **Before the stamp:** rollback is simply “do not switch over”; the database
  has not changed. Restore from backup only if another operation changed it.
- **After the stamp:** stamping writes one row in `alembic_version` and nothing
  else. Rolling the application back requires redeploying the previous release
  and, if its revision id must exist again, restoring the pre-squash database
  backup. The authoritative recovery path is **backup restore**, not a
  down-migration.
- **Do not run `alembic downgrade base` in production.** The baseline's
  downgrade drops every application table with `CASCADE`; it exists for
  disposable test databases only. It deliberately leaves the extensions in
  place.
- **Old revision files:** the tag is the recovery source for the old chain:

  ```bash
  git fetch --tags
  git show pre-migration-squash-20261019:apps/api/migrations/versions/20261020_0100_purchase_return_debit_note.py
  ```

  To run the old chain against a scratch database, create a worktree at the tag
  and point `DATABASE_SYNC_URL` at an empty database.

## Historical data work is preserved

The old chain contained data-only work in addition to DDL. It is preserved in
the tag and remains applied in every stamped database:

- FIFO opening-layer creation and correction, plus scrap cost-layer backfill.
- MFA-secret invalidation.
- Item-attribute backfill before the old text column was dropped.
- Report-snapshot cache purges for renamed/new report keys.
- Permission grants for item attributes, brands/manufacturers, cost centres,
  vendor portal, shifts, inventory operations and later modules.
- Conditional dealer-role inserts, which insert nothing on an empty database.

A fresh database has no rows for these statements to transform, so the baseline
does not replay them; an existing database is stamped so its already-transformed
rows stay exactly as they are. The SQL itself remains recoverable at the tag for
audit.

## Next migration

Every new revision chains from the baseline:

```python
down_revision = "20261021_0100"
```

`alembic heads` must print exactly one head after every change.
