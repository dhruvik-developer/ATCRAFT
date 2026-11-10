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
   the 2026-11-27 migration squash must record exactly revision `20261126_0100`,
   be verified on a clone, and then be stamped with
   `alembic stamp --purge 20261127_0100`; see
   `docs/implementation/migration-squash-20261127.md`. An earlier revision must
   be upgraded through the old chain at tag `pre-migration-squash-20261127`
   first (the October tag `pre-migration-squash-20261019` remains available for
   databases that never reached the November chain). Stamping records the
   baseline bridge only; run the normal guarded release afterwards to apply any
   later migrations through the current head. Never run the baseline upgrade
   against a populated pre-squash database.
6. Start the new release only after migration succeeds and wait for all health
   checks. The backend health check uses `/health/ready`, which checks both
   PostgreSQL and Redis. When it answers 503, the API's log says why: a
   `health.dependency_unready` line names the dependency, the error type and
   the address that refused, once per transition, and
   `health.dependency_ready` follows when it answers again.

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

## Production commissioning sequence

Commissioning has not started: no production host/domain or client environment
has been supplied. Keep the system in pre-production until each step has named
owners and retained evidence.

Use this as the single ordered commissioning checklist; the
[one-box runbook](../ONE-BOX-RUNBOOK.md) contains the detailed commands. Retain
an owner and evidence for every checked step.

1. [ ] **Provision the target.** Record the client-approved host/domain, DNS
   owner, environment owner, firewall/network boundaries, support contact and
   maintenance window. Confirm the selected host meets the documented storage
   and resource budget.
2. [ ] **Configure secrets, HTTPS and providers.** Generate independent
   production secrets outside Git; set the private env file, DNS, TLS and
   renewal, HTTPS redirect, HSTS, firewall and unknown-host rejection. Run
   `doctor` and the installed host-Nginx check. Keep Web Push, WhatsApp/SMS and
   courier tracking disabled until their approved accounts, allow-lists,
   consent/templates and delivery tests are ready. The optional public catalogue
   is not implemented in this revision and must remain unavailable. Retain the
   deployed defaults `MALWARE_SCANNER=clamav` and
   `MALWARE_SCAN_FAILURE_POLICY=closed`; the guarded deploy starts the
   digest-pinned ClamAV sidecar on the private network. Assign an owner to
   monitor scanner health and signature freshness; do not disable scanning or
   switch to fail-open without an approved security exception.
3. [ ] **Deploy, migrate and seed.** Deploy the exact verified `main` revision
   with the guarded script; verify Alembic reaches head before the baseline-only
   production seed/setup. Do not load demo data into the client database.
4. [ ] **Configure company schedules and automation.** In
   **Administration > Settings**, review sales `auto_plan_on_confirm` and the
   PO/requisition approval thresholds. In **Planning > MRP**, review the
   per-company nightly schedule (enablement, active warehouse scope, horizon
   and safety-stock treatment); changing it requires `mrp.run`. Record the
   resulting configuration. Configure the deployment's `MRP_SCHEDULE_CRON`
   (default 02:00 Asia/Kolkata) and smart-alert cadence. Do not enable an
   unapproved provider or the absent public catalogue.
5. [ ] **Verify workers, beat and health.** Confirm the batch worker consumes
   `default,mrp,reports`, the notification worker consumes `notifications`,
   and exactly one Celery beat runs for the deployment. Confirm ClamAV is
   healthy, run the deployment health check and confirm `/health/ready` checks
   PostgreSQL and Redis.
6. [ ] **Complete first-admin and role setup.** Change the one-time seeded
   administrator password immediately, enroll MFA according to client policy,
   verify password reset delivery, create named users and assign least-privilege
   roles. Under the shipped `MFA_REQUIRED_ROLES=SUPER_ADMIN,ADMIN,ACCOUNTS`
   every holder of those three roles must enrol an authenticator before any
   business screen opens (docs/assumptions.md §MFA-1); tell them before they
   first sign in, or set the population the client chose. Review the
   [new-permission grant matrix](#programme-permissions-by-role-preset) before
   granting client-owned roles.
7. [ ] **Prove off-box recovery.** Configure encrypted database and storage
   backups to a separate failure domain with client-approved retention. Restore
   both into an isolated target; verify schema, row counts, document hashes and
   file permissions, then retain the drill report and operator sign-off.
8. [ ] **Measure load and concurrency.** Agree workload and acceptance
   thresholds with the client, then exercise representative concurrent reads
   and writes across purchasing, allocation, stock posting, QC, dispatch and
   invoicing. Record latency, error rate, database/worker saturation and fixes;
   do not invent a capacity sign-off threshold.
9. [ ] **Run restricted-role UAT.** Use real staff, vendor and dealer principals
   with least-privilege roles. Verify permitted and refused actions, tenant
   isolation, approvals, stock traceability, finance totals and recovery from
   expected errors. Retain the signed scenario results.
10. [ ] **Complete browser and handbook verification.** Run full browser
    journeys, accessibility checks and the read-only route sweep against the
    release candidate. Recapture and visually review handbook screens using
    their bound staff/vendor/dealer principals. Confirm expected routes, pages
    and captures have zero failures.
11. [ ] **Authorize go-live.** Re-run release checks against the final revision,
    close or formally accept open assumptions and external-provider gates,
    verify monitoring/on-call ownership, and obtain client and operator
    sign-off. Retain the revision, CI runs, restore report, UAT evidence and
    completed checklist.

### Programme permissions by role preset

The table lists every permission added since the original permission review
baseline (`cdac4e0`) and the built-in preset that receives it. `ADMIN` and
`SUPER_ADMIN` receive all 35 additions. System vendor/dealer roles receive the
portal rights shown below. New companies receive current presets; existing
client-owned non-system roles are not silently expanded. An administrator must
grant only the listed rights needed by each existing role in **Settings →
Roles**.

The 35 added permission strings are: `brand.manage`, `brand.view`,
`collection.manage`, `collection.view`, `cost_centre.manage`,
`cost_centre.view`, `credit_note.cancel`, `credit_note.create`,
`credit_note.view`, `dealer_portal.account`, `dealer_portal.browse`,
`dealer_portal.order`, `dealer_portal.return`, `internal_issue.approve`,
`internal_issue.create`, `inventory.batch_manage`, `inventory.scrap_dispose`,
`manufacturer.manage`, `manufacturer.view`, `promotion.manage`,
`promotion.view`, `sales_invoice.cancel`, `sales_order.discount_override`,
`sales_return.create`, `sales_return.view`, `shift.manage`, `shift.view`,
`shift_roster.manage`, `shift_roster.view`, `vendor_portal.access`,
`vendor_portal.invoice`, `vendor_portal.job_work`, `vendor_portal.quality`,
`vendor_portal.respond` and `vendor_portal.ship`.

| Preset | New permissions in the preset |
|---|---|
| `SUPER_ADMIN`, `ADMIN` | All 35 strings listed above |
| `PLANT_MANAGER` | `cost_centre.view`, `internal_issue.approve`, `inventory.batch_manage`, `sales_return.create`, `sales_return.view`, `shift.manage`, `shift.view`, `shift_roster.manage`, `shift_roster.view` |
| `STORE_KEEPER` | `internal_issue.create`, `inventory.batch_manage`, `inventory.scrap_dispose`, `sales_return.create`, `sales_return.view` |
| `PURCHASE_MANAGER` | `brand.view`, `cost_centre.view`, `manufacturer.view`, `sales_return.create`, `sales_return.view` |
| `PURCHASE_EXECUTIVE` | `brand.view`, `cost_centre.view`, `manufacturer.view`, `sales_return.create`, `sales_return.view` |
| `PRODUCTION_SUPERVISOR` | `cost_centre.view`, `inventory.batch_manage`, `sales_return.create`, `sales_return.view`, `shift.view`, `shift_roster.manage`, `shift_roster.view` |
| `QC_INSPECTOR` | `inventory.batch_manage` |
| `DISPATCH_MANAGER` | None |
| `SALES_EXECUTIVE` | `brand.view`, `collection.manage`, `collection.view`, `credit_note.view`, `promotion.manage`, `promotion.view`, `sales_return.create`, `sales_return.view` |
| `ACCOUNTS` | `collection.manage`, `collection.view`, `cost_centre.manage`, `cost_centre.view`, `credit_note.cancel`, `credit_note.create`, `credit_note.view`, `sales_invoice.cancel` |
| `PLANNER` | `brand.view`, `cost_centre.view`, `shift.view`, `shift_roster.view` |
| `DESIGN_ENGINEER` | `brand.manage`, `brand.view`, `manufacturer.manage`, `manufacturer.view` |
| `AUDITOR` | `brand.view`, `collection.view`, `cost_centre.view`, `credit_note.view`, `manufacturer.view`, `promotion.view`, `sales_return.view`, `shift.view`, `shift_roster.view` |
| `VENDOR_OWNER` | `vendor_portal.access`, `vendor_portal.invoice`, `vendor_portal.job_work`, `vendor_portal.quality`, `vendor_portal.respond`, `vendor_portal.ship` |
| `VENDOR_MANAGER` | `vendor_portal.access`, `vendor_portal.job_work`, `vendor_portal.respond`, `vendor_portal.ship` |
| `VENDOR_DISPATCH_MANAGER` | `vendor_portal.access`, `vendor_portal.ship` |
| `VENDOR_QC_CONTACT` | `vendor_portal.access`, `vendor_portal.quality` |
| `VENDOR_ACCOUNT_MANAGER` | `vendor_portal.access`, `vendor_portal.invoice` |
| `DEALER_OWNER` | `dealer_portal.account`, `dealer_portal.browse`, `dealer_portal.order`, `dealer_portal.return` |
| `DEALER_BUYER` | `dealer_portal.browse`, `dealer_portal.order`, `dealer_portal.return` |
| `DEALER_ACCOUNTS` | `dealer_portal.account`, `dealer_portal.browse` |

`sales_order.discount_override` is intentionally not present in any
client-owned preset. Grant it only after the customer's discount authority and
approval policy are documented.

## Scaling order

Fix SQL and indexes first, then add safe Redis caching, move heavy work to
Celery, increase resources, and separate infrastructure only when measurements
justify it. Kubernetes or microservices are not production-readiness features.
