# Production deployment

The canonical operational procedure is
[`ONE-BOX-RUNBOOK.md`](../ONE-BOX-RUNBOOK.md). It covers the two independent
deployments, host Nginx/TLS, backup and restore, isolation verification, and
handover. Do not substitute a bare `docker compose up` command: the guarded
deployment script supplies the project isolation boundary and release identity.

The repository keeps only `main` and `develop`, synchronized to the same tested
revision. Deployment names remain `main` and `dev`, with `DEPLOY_BRANCH=main`
and `DEPLOY_BRANCH=develop` in their templates. The deployment name and the
configured Git branch are separate; `dev` requires no extra branch.

## Release workflow

```bash
cd /srv/atcraft-main
git fetch origin
git pull --ff-only origin main

./scripts/deploy.sh main doctor
./scripts/deploy.sh main up
./scripts/deploy.sh main health
```

`up` is intentionally an ordered release transaction:

1. Verify the expected branch, a clean working tree, a `HEAD` that is the
   pushed upstream commit, real hostnames, strong deployment configuration,
   and a private env-file mode.
2. Build application images from refreshed, patch-pinned bases while the old
   release is still serving. Images are tagged and labelled with the exact Git
   revision.
3. Stop Nginx, API, frontend, the batch and notification Celery workers, and
   Celery beat so no request or background task writes during the schema transition.
4. Take a PostgreSQL dump when this is an existing deployment.
5. Update/start PostgreSQL and Redis. A fresh database runs
   `alembic upgrade head` from the new backend image. A database created before
   the 2026-10-19 migration squash must record exactly revision `20261020_0100`,
   be verified on a clone, and then be stamped with
   `alembic stamp --purge 20261021_0100`; see
   `docs/implementation/migration-squash-20261021.md`. An earlier revision must
   be upgraded through the old chain at tag `pre-migration-squash-20261019`
   first. Stamping records the baseline bridge only; run the normal guarded
   release afterwards to apply all later migrations through the current head.
   Never run the baseline upgrade against a populated pre-squash database.
6. Start the new release only after migration succeeds and wait for all health
   checks. The backend health check uses `/health/ready`, which checks both
   PostgreSQL and Redis.

If migration fails, application traffic remains stopped and the script prints
the backup path. Inspect or restore deliberately; do not start new code against
an unverified schema.

This sequence provides correctness with a short maintenance window. Schema
changes should still use expand/contract design when uptime requirements grow.

Before promotion, run the shared quality gate (`make check` or
`.\make.ps1 check`) and the additional CI/security/browser checks described in
[`development-standards.md`](development-standards.md). Generated OpenAPI,
Postman and frontend contract types must match the release source. A green local
gate does not replace the backup, isolation, real-provider and restore checks
below.

## Storage sizing

A 100 GB server does not provide 100 GB to application data. A reasonable
initial budget is:

| Consumer                 | Reserve |
| ------------------------ | ------: |
| OS and packages          |  ~15 GB |
| Docker images and layers |  ~10 GB |
| PostgreSQL data          |  ~30 GB |
| PostgreSQL WAL           |   ~5 GB |
| Uploaded files           |  ~20 GB |
| Logs                     |   ~5 GB |
| Working headroom         |  ~15 GB |

Alert at 80% filesystem usage. Production Compose rotates container logs, but
database growth, backups, uploads, and old image layers still need monitoring.

## Reviewing a migration

- Check whether any statement takes `ACCESS EXCLUSIVE` on a large table.
- Prefer nullable additions, batched backfills, and expand/contract changes.
- Verify existing rows receive correct values and constraints validate safely.
- Measure the migration against realistic row counts.
- State explicitly whether downgrade loses information.
- Never edit the deployed schema by hand.

## Backups

```bash
./scripts/deploy.sh main backup
./scripts/deploy.sh main backup-verify
```

A local dump is only the first copy. Schedule database and `storage-data`
backups, encrypt and copy them off-box, enforce retention, and rehearse a full
restore. The runbook contains exact commands and cautions.

The guarded `restore` command builds the checked-out immutable release before
the outage, restores the archive transactionally, upgrades the restored schema
to Alembic head, and exposes traffic only after the application tier is
healthy. A migration failure leaves application traffic stopped for operator
recovery.

Runtime documents belong in volumes, not Git. Both `storage/**` and
`apps/api/storage/**` are ignored. If real documents appeared in an older Git
commit, coordinate a history rewrite and invalidate old clones/artifacts; a
normal deletion commit does not remove prior disclosure.

## Operations

```bash
./scripts/deploy.sh main ps
./scripts/deploy.sh main health
./scripts/deploy.sh main logs backend
./scripts/deploy.sh main migrate-status
```

`/health/ready` and `/metrics` are available only through the loopback-bound
deployment listener. Host Nginx returns 404 for them publicly. Unknown Host and
TLS SNI values are rejected by `infrastructure/nginx/host/00-default.conf`.

## Go-live gate

Security:

- [ ] `doctor` succeeds with real `CORS_ORIGINS`, `ALLOWED_HOSTS`,
      `PUBLIC_WEB_URL`, `EMAIL_FROM_ADDRESS` and `SMTP_HOST`; no `example.com`,
      localhost CORS origin, wildcard host, or weak secret.
- [ ] `./scripts/check-host-nginx.sh --installed` passes on the box.
- [ ] `JWT_SECRET` and `CREDENTIAL_ENCRYPTION_KEY` are independently generated,
      differ between deployments, and the credential key is backed up securely.
- [ ] TLS, HTTP redirect, HSTS, certificate renewal, firewall, and the default
      unknown-host rejection have been verified.
- [ ] PostgreSQL and Redis publish no host port; deployment Nginx binds only to
      its intended loopback port.
- [ ] Seeded administrator and password-reset flows have been exercised.
- [ ] Public `/docs`, `/redoc`, `/openapi.json`, `/metrics`, and `/health/*`
      return 404.

Data and operations:

- [ ] Alembic current revision matches head and the audit immutability trigger
      is installed.
- [ ] Automated encrypted off-box database and storage backups run with a
      retention policy, and a restore drill has succeeded.
- [ ] Container readiness, log rotation, disk alerts, error reporting, metrics,
      and an on-call owner are in place.
- [ ] The end-to-end manufacturing journey, real-role permissions, and expected
      concurrency have been tested against the release candidate.

## Scaling order

Fix SQL and indexes first, then add safe Redis caching, move heavy work to
Celery, increase resources, and separate infrastructure only when measurements
justify it. Kubernetes or microservices are not production-readiness features.
