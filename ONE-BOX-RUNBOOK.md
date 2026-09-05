# ONE-BOX RUNBOOK

Two independent deployments of the Atcraft ERP on a single host, plus the local
development setup. Every command here has been run; where a command is
destructive it says so before the command, not after.

If you read one thing, read [What keeps the two deployments
apart](#what-keeps-the-two-deployments-apart). The failure this architecture
exists to prevent is silent — two deployments sharing one database come up
perfectly healthy — so the isolation is verified by a script rather than by
inspection.

---

## Contents

1. [Architecture](#architecture)
2. [What keeps the two deployments apart](#what-keeps-the-two-deployments-apart)
3. [Local development](#local-development)
4. [Production setup, from a bare host](#production-setup-from-a-bare-host)
5. [Environment variables](#environment-variables)
6. [Everyday operations](#everyday-operations)
7. [Deploying a change](#deploying-a-change)
8. [Migrations](#migrations)
9. [Backup and restore](#backup-and-restore)
10. [Verifying isolation](#verifying-isolation)
11. [Troubleshooting](#troubleshooting)
12. [Handover checklist](#handover-checklist)

---

## Architecture

```
                          the internet
                                │
                         :80  :443
                                │
                  ┌─────────────▼──────────────┐
                  │   HOST NGINX (systemd)     │   TLS terminates here, once.
                  │   /etc/nginx/conf.d/       │   Certificates renew here, once.
                  │     atcraft-main.conf      │
                  │     atcraft-dev.conf       │
                  └──────┬──────────────┬──────┘
       erp.example.com   │              │   dev.erp.example.com
                         │              │
            127.0.0.1:8081         127.0.0.1:8082      loopback only —
                         │              │              nothing off-box can
   ══════════════════════▼═══════  ═════▼════════════   reach these directly
    DEPLOYMENT 1  "main"            DEPLOYMENT 2  "dev"
    project: atcraft-main           project: atcraft-dev
    branch:  main                   branch:  dev
   ────────────────────────────    ────────────────────────────
    nginx      (app router)         nginx      (app router)
    frontend   (static SPA)         frontend   (static SPA)
    backend    (gunicorn x4)        backend    (gunicorn x2)
    celery-worker                   celery-worker
    celery-beat                      celery-beat
    postgres  ── volume              postgres  ── volume
    redis     ── volume              redis     ── volume
              ── storage volume                ── storage volume
   ════════════════════════════    ════════════════════════════
    network: atcraft-main_internal  network: atcraft-dev_internal
                    ╲                        ╱
                     ╲ no route between them ╱
```

Both deployments run the **same** compose files. Everything that differs
between them lives in `deploy/main.env` and `deploy/dev.env`.

| | Deployment 1 | Deployment 2 |
|---|---|---|
| name | `main` | `dev` |
| compose project | `atcraft-main` | `atcraft-dev` |
| git branch | `main` | `dev` |
| loopback port | `8081` | `8082` |
| domain | `erp.example.com` | `dev.erp.example.com` |
| `APP_ENV` | `production` | `staging` |
| database | `atcraft_erp` in `atcraft-main_postgres-data` | `atcraft_erp` in `atcraft-dev_postgres-data` |
| Redis | `atcraft-main_redis-data` | `atcraft-dev_redis-data` |
| uploads | `atcraft-main_storage-data` | `atcraft-dev_storage-data` |
| gunicorn workers | 4 | 2 |
| Postgres RAM | `shared_buffers=1GB`, limit 4G | `shared_buffers=256MB`, limit 1G |

The database **name** is the same in both. That is deliberate and harmless:
they are different clusters, in different containers, on different volumes, on
different networks. Nothing in either deployment can resolve the other's
hostname, so there is no context in which the shared name is ambiguous.

### Why there is an nginx in each deployment *and* one on the host

They do different jobs, and putting both jobs in one place breaks something:

- **Host nginx** owns TLS, the public hostnames, HSTS and the HTTP→HTTPS
  redirect. Two containers cannot both bind `:443`, and a certificate should be
  renewed in exactly one place.
- **Deployment nginx** owns `/api` routing, per-endpoint rate limits, the
  response security headers, the SPA fallback and asset caching. Keeping it
  means each deployment is complete and testable on its own — `curl
  http://127.0.0.1:8081/healthz` answers before the host nginx is configured at
  all — and the deployment behaves the same in the isolation test as it does in
  production.

---

## What keeps the two deployments apart

The boundary is the **Compose project name**, and everything else follows from
it. Compose derives container names, the bridge network and the volume names
from the project, so `atcraft-main` and `atcraft-dev` get:

```
atcraft-main_internal        atcraft-dev_internal          separate networks
atcraft-main_postgres-data   atcraft-dev_postgres-data     separate volumes
atcraft-main_redis-data      atcraft-dev_redis-data
atcraft-main_storage-data    atcraft-dev_storage-data
```

Inside deployment 1, the hostname `postgres` resolves to deployment 1's cluster
and to nothing else — DNS for a project exists only on that project's network.
This is why both deployments can use identical service names and identical
database names without ambiguity.

Four things enforce it, and each replaces a failure that produces a *working*
system:

1. **`docker-compose.yml` requires `COMPOSE_PROJECT_NAME`.** A bare
   `docker compose up` in this directory now fails outright. Without that,
   Compose derives the project from the directory name — both deployments land
   in the same project, and the second one mounts and migrates the first one's
   database while reporting every container healthy.
2. **No `container_name:` anywhere.** A fixed container name is claimed
   host-wide, so the second deployment cannot start at all.
3. **`scripts/deploy.sh` is the only entry point.** It reads
   `deploy/<name>.env`, takes the project name from it, passes it explicitly
   with `-p` on every command, and refuses to run if the two env files share a
   project name, a port, a JWT secret or a credential-encryption key.
4. **Separate signing and encryption secrets per deployment.** The last
   boundary, and the only one
   that survives a network misconfiguration: a session minted by the dev
   deployment must not authenticate against the client's data. `JWT_ISSUER` and
   `JWT_AUDIENCE` differ too, so a cross-deployment token is rejected as
   wrong-audience rather than merely failing to verify.

Prove it, don't trust it:

```bash
./scripts/verify-isolation.sh
```

---

## Local development

The local stack is a **third** stack, separate from both deployments. It is the
only one that publishes a database port, because the host-run API, the host-run
Vite server and pytest all need to reach Postgres.

Its compose project is `atcraft-erp`, set by the Makefile and `make.ps1` —
deliberately the historical name, so the volumes an existing checkout already
has, and the demo data in them, stay where they are.

Its containers are `atcraft-erp-postgres-1` and `atcraft-erp-redis-1`, publishing
`127.0.0.1:${POSTGRES_PORT}` and `127.0.0.1:${REDIS_PORT}` — 5432 and 6379
unless `.env` overrides them, which the First-time section below recommends
(5433/6380) because a host Postgres often already holds 5432. `.\make.ps1 dev`
prints the resolved ports.

### First time

```bash
git clone <repo> atcraft && cd atcraft
cp .env.example .env
```

Then edit `.env` and set, at minimum:

```ini
POSTGRES_PASSWORD=<anything, this is a laptop>
JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(64))">
POSTGRES_PORT=5433          # 5432 is often already taken by a host Postgres
REDIS_PORT=6380
DATABASE_URL=postgresql+asyncpg://atcraft:<password>@localhost:5433/atcraft_erp
DATABASE_SYNC_URL=postgresql+psycopg://atcraft:<password>@localhost:5433/atcraft_erp
REDIS_URL=redis://localhost:6380/0
CELERY_BROKER_URL=redis://localhost:6380/1
CELERY_RESULT_BACKEND=redis://localhost:6380/2
```

Install dependencies:

```bash
# Linux / macOS
make install

# Windows
.\make.ps1 install
```

### Hybrid mode — the daily driver

Postgres and Redis in Docker; API and web on the host with hot reload.

```bash
make dev                 # Linux / macOS
.\make.ps1 dev           # Windows
```

That brings up infrastructure and then runs both dev servers. First time only,
in another terminal:

```bash
make migrate             # create the schema
make seed                # roles, permissions, units of measure, warehouse types
make demo-data           # a working business to click around in
```

- API: <http://localhost:8000>, docs at `/docs`
- Web: <http://localhost:5173>

### Full-Docker development

Everything in containers, hot reload through the deployment nginx on
<http://localhost>:

```bash
make dev-docker
```

### The single demo dataset

There is exactly one database with demo data in it: `atcraft_erp` in the local
stack. `atcraft_erp_test` alongside it is created and dropped by pytest and is
not a dataset.

```bash
make db-fresh       # DESTROYS the local database, rebuilds it with demo data
make db-handover    # DESTROYS it, rebuilds with the BASELINE ONLY - no demo rows
```

`db-fresh` rebuilds rather than deletes because the demo rows span 33 tables
joined by foreign keys and only the parents carry a recognisable `DM-` code —
there is no reliable way to delete "just the demo data" afterwards. Run
`db-handover` before giving the client a database.

A full load registers **234 records across 34 kinds** and is idempotent: running
it again creates nothing. It prints what it skipped and why, which is worth
reading — several skips are the business rules refusing an unrealistic
combination rather than a failure.

### Tests

```bash
make test           # backend unit + integration + api
make test-web       # frontend unit tests
make check          # everything CI runs: contract, lint, typecheck, tests
```

---

## Production setup, from a bare host

Assumes Ubuntu 22.04/24.04 with a sudo user. Substitute your real hostnames for
`erp.example.com` and `dev.erp.example.com` throughout.

### 1. DNS

Point both names at the box before requesting certificates:

```
erp.example.com.       A   <host ip>
dev.erp.example.com.   A   <host ip>
```

### 2. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version          # must be v2.20 or newer
```

### 3. Firewall — only 80 and 443

Both deployments publish on loopback only, so this is defence in depth rather
than the only thing standing between the internet and Postgres.

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

### 4. Checkout and branches

Both deployments build from a working tree, so each needs its own checkout —
one repository cannot be on two branches at once.

```bash
sudo mkdir -p /srv && sudo chown "$USER" /srv

git clone <repo> /srv/atcraft-main
cd /srv/atcraft-main && git checkout main

git clone <repo> /srv/atcraft-dev
cd /srv/atcraft-dev && git checkout dev
```

`scripts/deploy.sh` refuses to build deployment `main` from a tree that is not
on `main`, and likewise for `dev`, so a forgotten `git checkout` cannot ship
the wrong code.

### 5. Generate each deployment's secrets

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main init
cd /srv/atcraft-dev  && ./scripts/deploy.sh dev  init
```

Each writes `deploy/<name>.env`, mode 600, with a freshly generated
`POSTGRES_PASSWORD`, `JWT_SECRET`, and `CREDENTIAL_ENCRYPTION_KEY`. No value is
printed or logged. The encryption key is independent so JWT rotation does not
invalidate enrolled authenticators. `init` refuses to overwrite an existing file.

Then edit each file and replace every `example.com` placeholder. Five keys
ship with one — the two public-hostname keys and three mail keys — and `doctor`
and `up` refuse the file while any of them still holds it, naming the key. The
values below are examples only:

```ini
# /srv/atcraft-main/deploy/main.env
CORS_ORIGINS=https://erp.yourcompany.com
ALLOWED_HOSTS=erp.yourcompany.com
PUBLIC_WEB_URL=https://erp.yourcompany.com
EMAIL_FROM_ADDRESS=no-reply@yourcompany.com
SMTP_HOST=smtp.yourcompany.com
SMTP_USERNAME=<relay account, if the relay authenticates>
SMTP_PASSWORD=<its password - both set, or both empty>

# /srv/atcraft-dev/deploy/dev.env
CORS_ORIGINS=https://dev.erp.yourcompany.com
ALLOWED_HOSTS=dev.erp.yourcompany.com
PUBLIC_WEB_URL=https://dev.erp.yourcompany.com
EMAIL_FROM_ADDRESS=no-reply-dev@yourcompany.com
SMTP_HOST=<a sandbox or test relay - never the client's production one>
SMTP_USERNAME=
SMTP_PASSWORD=
```

Mail is not optional in a deployment: a password-reset response must
correspond to a deliverable link, so `doctor` also requires
`EMAIL_PROVIDER=smtp`, `SMTP_SECURITY=starttls` or `tls`, an `https://`
`PUBLIC_WEB_URL`, and `SMTP_USERNAME`/`SMTP_PASSWORD` either both set or both
empty. The templates already carry the right `EMAIL_PROVIDER`, `SMTP_PORT` and
`SMTP_SECURITY`.

Check the result without starting anything:

```bash
./scripts/deploy.sh main doctor
```

**Never copy secrets between the two files.** `deploy.sh` refuses to run if the
`JWT_SECRET`s or `CREDENTIAL_ENCRYPTION_KEY`s match and warns if the database
passwords do.

### 6. Start both deployments

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main up
cd /srv/atcraft-dev  && ./scripts/deploy.sh dev  up
```

`up` runs the complete guarded release: it builds commit-addressed images while
the old release is live, stops application traffic, takes a pre-migration
backup when a database already exists, updates infrastructure, migrates, then
starts the new application and waits for dependency-aware readiness.

### 7. Seed the schemas — separately, per database

```bash
cd /srv/atcraft-main && ./scripts/deploy.sh main seed
cd /srv/atcraft-dev  && ./scripts/deploy.sh dev  seed
```

Optionally give the dev deployment a demo business to test against:

```bash
cd /srv/atcraft-dev && ./scripts/deploy.sh dev demo-data
```

Do **not** load demo data into deployment 1. `deploy.sh` will demand a typed
confirmation if you try, because `APP_ENV=production` there.

### 8. Certificates

Issue the certificates *before* the host configs go in. The `:443` server
blocks in `atcraft-main.conf` and `atcraft-dev.conf` name
`/etc/letsencrypt/live/<hostname>/`, so on a bare host `nginx -t` fails on
those files every time until the certificates exist — not conditionally — and
a reload chained behind it never runs.

What answers the ACME challenge at this point is the packaged default site
(`server_name _`, root `/var/www/html`), which serves any hostname the moment
the package is installed. The symlink lets it serve the challenge from
`/var/www/certbot`, the directory the atcraft `:80` blocks serve later. That
matters: certbot records the webroot a certificate was issued with and renews
against it, so a certificate issued against `/var/www/html` would fail every
renewal once step 9 has replaced that site.

```bash
sudo apt-get update && sudo apt-get install -y nginx certbot
sudo mkdir -p /var/www/certbot
sudo ln -sfn /var/www/certbot/.well-known /var/www/html/.well-known

sudo certbot certonly --webroot -w /var/www/certbot -d erp.yourcompany.com \
  --agree-tos -m ops@yourcompany.com --no-eff-email
sudo certbot certonly --webroot -w /var/www/certbot -d dev.erp.yourcompany.com \
  --agree-tos -m ops@yourcompany.com --no-eff-email

ls /etc/letsencrypt/live/      # erp.yourcompany.com  dev.erp.yourcompany.com
```

Two `certonly` runs, not one with two `-d` flags. A single run makes one
lineage, named after the first domain; `atcraft-dev.conf` names
`/etc/letsencrypt/live/dev.erp.yourcompany.com/`, which that run never creates,
so `nginx -t` fails with "cannot load certificate" and both deployments stay
unreachable over TLS. The certificate paths are one of the three deliberate
differences between the two host configs.

### 9. Host nginx

```bash
sudo mkdir -p /etc/nginx/snippets
sudo cp /srv/atcraft-main/infrastructure/nginx/host/snippets/atcraft-proxy.conf \
        /etc/nginx/snippets/
sudo cp /srv/atcraft-main/infrastructure/nginx/host/00-default.conf \
        /srv/atcraft-main/infrastructure/nginx/host/atcraft-main.conf \
        /srv/atcraft-main/infrastructure/nginx/host/atcraft-dev.conf \
        /etc/nginx/conf.d/

# Real hostnames
sudo sed -i 's/erp\.example\.com/erp.yourcompany.com/g'         /etc/nginx/conf.d/atcraft-main.conf
sudo sed -i 's/dev\.erp\.example\.com/dev.erp.yourcompany.com/g' /etc/nginx/conf.d/atcraft-dev.conf

# Load the installed copies in a throwaway nginx before touching the live one:
# a fault here takes both deployments offline, at reload time
cd /srv/atcraft-main && ./scripts/check-host-nginx.sh --installed
```

`--installed` reads `/etc/nginx/conf.d` and `/etc/nginx/snippets`, generates
self-signed certificates at the exact paths the configs name, and runs
`nginx -t` plus the routing, port and hostname assertions. It also refuses any
installed config that still says `example.com`. Without the flag the script
checks the repository copies, which keep the placeholders — customising those
instead would dirty the working tree that `deploy.sh up` requires clean.

Remove the packaged default site. The supplied `00-default.conf` becomes the
only default and rejects unknown HTTP Host and TLS SNI values:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

sudo systemctl enable --now certbot.timer     # renewal
sudo certbot renew --dry-run                  # now through the :80 blocks and /var/www/certbot
```

### 10. Verify

```bash
cd /srv/atcraft-main
./scripts/deploy.sh main health
./scripts/deploy.sh dev health
./scripts/verify-isolation.sh

curl -sI https://erp.yourcompany.com     | head -1
curl -sI https://dev.erp.yourcompany.com | head -1
```

`verify-isolation.sh` needs both `deploy/main.env` and `deploy/dev.env`. With
two checkouts, copy the other deployment's env file in read-only to run it, or
run it from a directory that has both.

---

## Environment variables

### Per deployment — `deploy/<name>.env`

Consumed by Docker Compose interpolation. `(UNIQUE)` must differ between the
two deployments.

| Variable | Example | Notes |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | `atcraft-main` | **(UNIQUE)** The isolation boundary. Required. |
| `DEPLOY_BRANCH` | `main` | Branch this deployment tracks; `deploy.sh` enforces it. |
| `HTTP_PORT` | `8081` | **(UNIQUE)** Loopback port. Must match the host nginx `proxy_pass`. Required. |
| `APP_ENV` | `production` | `production` / `staging` / `development`. |
| `LOG_LEVEL` | `INFO` | |
| `CORS_ORIGINS` | `https://erp.example.com` | Comma-separated. Never `*` with credentials. |
| `ALLOWED_HOSTS` | `erp.example.com` | Hostnames only. Required; never `*`. |
| `EMAIL_PROVIDER` | `smtp` | Required; `doctor` refuses anything else. A deployment fails closed without working mail. |
| `PUBLIC_WEB_URL` | `https://erp.example.com` | The origin password-reset links point at. Must be `https://`; must not contain `example.com`. |
| `EMAIL_FROM_ADDRESS` | `no-reply@example.com` | Sender address. Required; must not contain `example.com`. |
| `EMAIL_FROM_NAME` | `Atcraft Manufacturing ERP` | Sender display name. |
| `SMTP_HOST` | `smtp.example.com` | The relay. Required; must not contain `example.com`. |
| `SMTP_PORT` | `587` | |
| `SMTP_SECURITY` | `starttls` | `starttls` or `tls`; `doctor` refuses anything else. |
| `SMTP_USERNAME` | *(empty)* | Only if the relay authenticates. Set both or neither. |
| `SMTP_PASSWORD` | *(empty)* | Paired with `SMTP_USERNAME`. |
| `SMTP_TIMEOUT_SECONDS` | `15` | |
| `POSTGRES_DB` | `atcraft_erp` | May match the other deployment. |
| `POSTGRES_USER` | `atcraft` | May match. |
| `POSTGRES_PASSWORD` | — | **(UNIQUE)** Required, no default. |
| `JWT_SECRET` | — | **(UNIQUE)** Required. Sharing it means a dev token works in production. |
| `CREDENTIAL_ENCRYPTION_KEY` | - | **(UNIQUE)** Required; encrypts MFA and reset-delivery credentials and must differ from `JWT_SECRET`. |
| `JWT_ISSUER` | `atcraft-erp-main` | **(UNIQUE)** |
| `JWT_AUDIENCE` | `atcraft-erp-main` | **(UNIQUE)** |
| `IMAGE_PREFIX` | `atcraft` | |
| `IMAGE_TAG` | *(derived)* | `deploy.sh` forces the exact 12-character Git revision. |
| `VITE_ERROR_REPORT_URL` | *(empty)* | Empty disables browser crash reporting. Use a same-origin collector path unless its HTTPS origin is deliberately added to the CSP. |
| `SENTRY_DSN` | *(empty)* | Empty disables scrubbed backend exception reporting. Use a different Sentry project for each deployment. |
| `PG_MAX_CONNECTIONS` | `200` | |
| `PG_SHARED_BUFFERS` | `1GB` | ~25% of this deployment's RAM allowance. |
| `PG_EFFECTIVE_CACHE_SIZE` | `3GB` | ~50–75% of it. |
| `PG_WORK_MEM` | `16MB` | |
| `PG_MAINTENANCE_WORK_MEM` | `256MB` | |
| `PG_LOG_MIN_DURATION_MS` | `500` | Slow-query threshold. |
| `PG_MEM_LIMIT` | `4G` | Container ceiling. |
| `REDIS_MAXMEMORY` | `1gb` | |
| `REDIS_MEM_LIMIT` | `1500M` | |
| `GUNICORN_WORKERS` | `4` | |
| `BACKEND_MEM_LIMIT` | `2G` | |
| `CELERY_CONCURRENCY` | `4` | |
| `WORKER_MEM_LIMIT` | `2G` | |

Both Postgres clusters share one box's RAM. Size them together: the sum of the
two `PG_MEM_LIMIT`s plus both backends and both workers must fit, with room for
the page cache.

### Fixed by the compose files, not by env

Set in `docker-compose.yml` / `docker-compose.prod.yml` so they cannot drift
between deployments: `DATABASE_URL`, `DATABASE_SYNC_URL`, `REDIS_URL`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `STORAGE_PATH`, `APP_DEBUG=false`,
`LOG_JSON=true`, `CACHE_ENABLED=true`, `RATE_LIMIT_ENABLED=true`.

The database and Redis URLs are built from `POSTGRES_*` and point at the
service names `postgres` and `redis`, which resolve on the deployment's own
network only. There is no host-side connection string to get wrong.

### Local development — `.env` at the repository root

Read by the app directly (`pydantic-settings`) and by Compose for the local
stack. See `.env.example`, which lists every setting with its purpose. Never
commit a populated one; `.gitignore` covers `.env` and `deploy/*.env`.

### Nothing secret reaches the browser

Only `VITE_*` variables are exposed to the frontend, and Vite inlines them into
the bundle at build time — everything in `dist/` is public and cached
indefinitely. Three of them exist: `VITE_API_BASE_URL` (a relative path),
`VITE_APP_RELEASE` and `VITE_ERROR_REPORT_URL`. The template's
`VITE_API_PROXY_TARGET` and `VITE_USE_POLLING` are read only by
`vite.config.ts` for the dev server and are never inlined.

`npm run build` fails if a credential reaches the bundle:

```bash
cd apps/web && npm run check:secrets
```

It scans the built output — after minification and after every substitution
Vite performs — for JWTs, private keys, database and Redis URLs carrying
passwords, cloud access keys, service tokens, and this project's own demo
passwords. Grepping the source is not the same check; the artefact is what
ships. The gate runs inside the frontend image build, so it applies to both
deployments.

---

## Everyday operations

Every command takes the deployment name. There is no default.

```bash
./scripts/deploy.sh main ps               # container status
./scripts/deploy.sh main health           # containers + HTTP + readiness detail
./scripts/deploy.sh main logs             # follow all
./scripts/deploy.sh main logs backend     # follow one
./scripts/deploy.sh main restart          # the full guarded release again - same as up
./scripts/deploy.sh main psql             # psql in this deployment's database
./scripts/deploy.sh main shell            # shell in this deployment's backend
./scripts/deploy.sh main doctor           # configuration, without starting anything
```

`restart` is not a bounce. It re-runs the guarded release, so it refuses a
dirty tree or a `HEAD` that is not the pushed upstream, rebuilds the images,
takes a backup and migrates — exactly what `up` does. Use it to pick up an
edited `deploy/<name>.env`; do not reach for it to "just restart" a box with
uncommitted changes on it, because it will refuse.

`down` keeps the data:

```bash
./scripts/deploy.sh main down             # containers removed, volumes kept
```

`destroy` does not, and asks first:

```bash
./scripts/deploy.sh main destroy          # requires typing "destroy main"
```

`deploy.sh down` never passes `-v`. The two commands differ by two characters
and one of them is unrecoverable.

### Operational endpoints

`/health/live`, `/health/ready` and `/metrics` are served on the deployment's
loopback port and return **404 through the host nginx**. Readiness names which
dependency is down; metrics publishes request rates. Both are for whoever runs
the box.

```bash
curl -s http://127.0.0.1:8081/health/ready
# {"status":"ready","checks":{"database":true,"redis":true}}
```

---

## Deploying a change

```bash
cd /srv/atcraft-main
git fetch origin
git status                                  # must be clean
git pull --ff-only origin main

./scripts/deploy.sh main doctor
./scripts/deploy.sh main up                 # backup + migrate happen before new traffic
./scripts/deploy.sh main health
```

`up` refuses a dirty tree, detached HEAD, wrong branch, insecure env-file mode,
placeholder host configuration, or a `HEAD` that is not the pushed upstream
commit. Images are tagged and labelled with the
exact commit, so the running artifact can be traced and an old commit's image
cannot be silently overwritten by a later branch build.

Normal flow is dev first:

```
feature branch → dev → deployment 2 → verify → merge to main → deployment 1
```

---

## Migrations

Each deployment migrates its own database. The command runs inside that
deployment's project, so it can only reach that deployment's Postgres — there
is no connection string to point at the wrong one.

```bash
./scripts/deploy.sh main migrate-status     # current revision + heads
```

Normal releases always use `up`, which owns stop, backup, migration, readiness,
and traffic ordering. Standalone migration is recovery-only: all application
services must already be stopped and the operator must set
`ALLOW_SCHEMA_RECOVERY=1` explicitly.

Rolling back asks for confirmation, because a downgrade that drops a column
destroys the data in it:

```bash
ALLOW_SCHEMA_RECOVERY=1 ./scripts/deploy.sh main downgrade 1
```

Authoring a migration is a development task, against the local stack:

```bash
make migration m="add packing slip reference"
# review the generated file - locking impact, data migration, rollback
make migrate
make test
```

`./scripts/deploy.sh main up` is the order of operations for every release. It
builds first, then stops HTTP and workers, takes a consistent backup, updates
Postgres/Redis patch images, runs `alembic upgrade head` from the new immutable
backend image, and only then starts new application containers. If migration
fails, application traffic remains stopped and the script prints the backup
path; new code is never served against the old schema.

The standalone `migrate` command is for recovery and operator inspection. A
normal release must not split `up` and `migrate` into separate steps.

---

## Backup and restore

### Take a backup

```bash
./scripts/deploy.sh main backup
# → backups/main/atcraft-main-atcraft_erp-20260903T140000Z.dump   (mode 400)
```

`pg_dump -Fc` — custom format, so it restores selectively and in parallel.
`pg_dump` runs inside the container, so no host port and no host-side password.
The file is written mode 400 because a dump is a complete copy of the business,
including every password hash.

**A dump on the machine it came from is not a backup.** Send it off the box:

```bash
scp backups/main/atcraft-main-*.dump backup-host:/backups/atcraft/main/
```

### Automate it

```bash
sudo tee /etc/cron.d/atcraft-backup >/dev/null <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
30 1 * * * ubuntu cd /srv/atcraft-main && ./scripts/deploy.sh main backup >> /var/log/atcraft-backup.log 2>&1
45 1 * * * ubuntu cd /srv/atcraft-dev  && ./scripts/deploy.sh dev  backup >> /var/log/atcraft-backup.log 2>&1
CRON
```

Add off-box copying and retention to the same job. Two deployments dumping
nightly with no retention will fill the disk, and a full disk stops Postgres.

### Restore

```bash
./scripts/deploy.sh main restore backups/main/atcraft-main-atcraft_erp-20260903T140000Z.dump
```

Requires typing `restore into main`. Runs `--single-transaction` with
`--clean --if-exists`, so it is all-or-nothing: a restore that fails part way
leaves the database as it was, rather than half-replaced.

`restore` stops the application tier before it writes and starts it again
afterwards — the same traffic gate as `up` — so no worker holds a connection
from before the restore. Before the maintenance window it builds the immutable
application images for the checked-out revision. After the dump is restored it
runs `alembic upgrade head`, and it resumes traffic only after the migrated
application tier is healthy. A separate `restart` is not needed, and would not
be a bounce anyway: it is the full guarded release.

### Prove the backups work

```bash
./scripts/deploy.sh main backup-verify
```

Dumps the live database, creates a scratch database in the same cluster,
restores into it, compares the table count and the row counts of
`companies`, `users`, `items`, `inventory_transactions` and `audit_logs`, then
drops the scratch database. The live database is never written to. It fails,
and says which line disagreed, when any count differs or when a table cannot
be counted on either side — a table the restore lost is not a match.

A backup nobody has restored is a hypothesis. Run this monthly.

### Disaster recovery, from nothing

```bash
git clone <repo> /srv/atcraft-main && cd /srv/atcraft-main && git checkout main
cp <saved copy of deploy/main.env> deploy/main.env && chmod 600 deploy/main.env
./scripts/deploy.sh main up
./scripts/deploy.sh main restore /backups/atcraft/main/<latest>.dump
./scripts/deploy.sh main health
```

Keep `deploy/main.env` somewhere safe and off the box. Without `JWT_SECRET` the
dump still restores, but every session is invalidated; without
`POSTGRES_PASSWORD` a **restored volume** cannot be opened at all — the password
lives in the cluster, not in the env file, so a new random one will not match.

Uploaded documents live in the `storage-data` volume, not in the database
dump. Back them up too:

```bash
docker run --rm -v atcraft-main_storage-data:/data:ro -v "$PWD/backups/main:/out" \
  alpine tar czf /out/storage-$(date -u +%Y%m%dT%H%M%SZ).tar.gz -C /data .
```

Runtime storage is ignored at both `storage/**` and `apps/api/storage/**`.
Never force-add a generated PDF, CSV, upload, export, or attachment. If real
customer documents were ever pushed before this rule was added, removing them
in a later commit is not sufficient: treat the remote as disclosed, arrange a
coordinated `git filter-repo` history rewrite, and expire every old clone and
artifact. Do not rewrite shared history as an unattended deploy step.

---

## Verifying isolation

```bash
./scripts/verify-isolation.sh
```

Both deployments must be up. It attempts each crossing **from inside
deployment 1's own backend container** — where a bug, a mistargeted migration
or a compromised dependency would run from — and requires each attempt to fail:

1. **Distinct infrastructure** — project names, volume mountpoints and bridge
   subnets differ, and no container is attached to both networks.
2. **Database** — deployment 2's container hostnames do not resolve; a TCP
   connection to its Postgres IP does not open; and a connection attempt
   *holding deployment 2's real password* still fails. The boundary is the
   network, not the credential.
3. **Redis** — same, in both directions. A shared Redis would mean one
   deployment's Celery worker running real business logic against the other's
   database.
4. **Host exposure** — neither Postgres nor Redis publishes a host port.
5. **Serving** — each loopback port answers ready, and a marker row written to
   deployment 1's database is absent from deployment 2's. Health checks cannot
   tell you *which* database answered; a marker row can.
6. **Tokens** — the two deployments sign with different secrets and declare
   different audiences.
7. **A real session, replayed** — opt-in. Signs in to both deployments and
   replays each token against the other, which must answer 401. Section 6
   compares the inputs; this observes the outcome, and they are not the same
   claim.

```bash
ATCRAFT_MAIN_PW='...' ATCRAFT_DEV_PW='...' ./scripts/verify-isolation.sh
```

The passwords are read from the environment and never printed. Without them
section 7 reports SKIP — and a skip is not a pass.

A clean run reports **22 passed, 0 failed, 0 skipped**.

Run it after any change to a compose file, an env file or the host nginx.

---

## Troubleshooting

### `COMPOSE_PROJECT_NAME must be set`

You ran `docker compose` directly. Use `./scripts/deploy.sh <main|dev> …`, or
for the local stack `make dev` (hybrid) or `make dev-docker` (full Docker) —
`.\make.ps1 dev` / `.\make.ps1 dev-docker` on Windows. The requirement is
deliberate — see [What keeps the two deployments
apart](#what-keeps-the-two-deployments-apart).

### `deployment 'dev' tracks branch 'dev' but the working tree is on 'main'`

`git checkout dev`, or use a second checkout. To override for one command:
`DEPLOY_BRANCH_OVERRIDE=1 ./scripts/deploy.sh dev up`.

### `HEAD is not the tested upstream revision`

The checkout is ahead of, behind, or beside what was pushed — typically a
hot-fix committed on the box, or a `git fetch` without the pull. Images are
tagged by commit, and the tag is only a provenance claim if that commit is the
one CI tested. Push the commit and let CI pass, then `git pull --ff-only`.
`DEPLOY_REVISION_OVERRIDE=1 ./scripts/deploy.sh main up` exists for a
documented emergency only; a branch with no upstream at all is refused the
same way.

### `both deployments share JWT_SECRET`

`deploy.sh` refuses to continue. Generate a new secret for one of them and
restart it — everyone signed in to that deployment is logged out.

The same refusal applies to `CREDENTIAL_ENCRYPTION_KEY`. Generate an independent
value, but do not rotate a credential-encryption key after MFA enrollment unless
you also have a planned data re-encryption procedure.

### The second deployment will not bind its port

```bash
sudo ss -ltnp | grep -E ':(8081|8082)'
```

Two deployments with the same `HTTP_PORT`. `deploy.sh` catches this when both
env files are present in one checkout; with two checkouts it cannot see the
other file, so check by hand.

### `Worker failed to boot` with an `IndexError` from pathlib

Fixed. `src/core/config.py` found the repository root by counting directories
up from its own file, which is right in a checkout and wrong in the image,
where `src/` sits directly at `/app`. It raised during import, before any
logger existed. It could not appear in development because hybrid mode runs
from the checkout — the deployed image was the first thing ever to import that
module from an installed layout. `tests/unit/test_config_roots.py` now tests
both layouts as data.

### Frontend container restarting: `open() "/run/nginx.pid" failed (13: Permission denied)`

Fixed. The image ran `chown -R nginx:nginx /var/run`, which does nothing
useful: `/var/run` is a symlink to `/run`, and `chown -R` does not traverse
symlinks, so it retargeted the link and left `/run` owned by root. nginx logged
its version and worker method, then died on the last step before serving. The
Dockerfile now creates the pid file and gives nginx that one file.

### `container atcraft-main-celery-beat-1 is unhealthy`

Fixed. Beat inherited the api image's `HEALTHCHECK` — an HTTP probe of
`/health/live` — and serves no HTTP, so it was permanently unhealthy: `up
--wait` never returned and every monitor showed a broken container that was
working perfectly. It now checks the freshness of its own schedule file, which
is the one honest signal that beat has stopped scheduling while its process is
still alive.

### 502 from the host nginx

The deployment is down or unhealthy:

```bash
./scripts/deploy.sh main health
curl -s http://127.0.0.1:8081/health/ready
sudo tail -50 /var/log/nginx/atcraft-main.error.log
```

### Rate limiting behaves as though every visitor is one client

Check that the deployment nginx still has its `set_real_ip_from` block. The
application uses `X-Real-IP` as the rate-limit identity and as the audit login
IP. Behind the host nginx, without the realip module, `$remote_addr` is the
Docker bridge gateway — identical for every visitor — so per-IP rate limiting
collapses into one shared bucket and every audit row records the same useless
address.

### `pg_isready` fails but the container is running

Almost always `POSTGRES_PASSWORD` changed in the env file after the cluster was
initialised. The password lives in the volume. Either restore the old value, or
`ALTER USER` inside the running cluster:

```bash
./scripts/deploy.sh main psql -c "ALTER USER atcraft WITH PASSWORD '<new>';"
```

### Disk full

```bash
docker system df
docker image prune -f                 # dangling images only
du -sh backups/*
```

Never `docker system prune --volumes`. It deletes named volumes not attached to
a running container — which includes a stopped deployment's entire database.

---

## Handover checklist

Before the client takes over:

- [ ] `./scripts/deploy.sh main doctor` — clean
- [ ] `./scripts/deploy.sh main health` — all containers healthy, readiness 200
- [ ] `./scripts/deploy.sh dev health` — same
- [ ] `./scripts/verify-isolation.sh` — 0 failed, 0 skipped
- [ ] `deploy/main.env` and `deploy/dev.env` are mode 600, not in git, and a
      copy of each is stored off the box
- [ ] `CORS_ORIGINS`, `ALLOWED_HOSTS`, `PUBLIC_WEB_URL`, `EMAIL_FROM_ADDRESS`
      and `SMTP_HOST` hold the real values in both env files — `doctor` names
      any key that still says `example.com`
- [ ] Host nginx: `./scripts/check-host-nginx.sh --installed` passes, which
      also proves neither installed config contains `example.com`
- [ ] `JWT_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD` differ
      between the two deployments; each credential key also differs from its JWT key
- [ ] Deployment 1 contains **no demo data** — `make db-handover` on the source
      database, or verify: `./scripts/deploy.sh main psql -tAc "select count(*) from customers where code like 'DM-%'"` returns 0
- [ ] The seeded administrator's password has been changed
- [ ] `curl -sI "https://$ERP_HOST"` returns 200 with
      `Strict-Transport-Security`
- [ ] `curl -s "https://$ERP_HOST/metrics"` returns 404
- [ ] `curl -s "https://$ERP_HOST/health/ready"` returns 404
- [ ] an unknown Host/SNI is rejected by `00-default.conf`, not routed to main
- [ ] `sudo certbot renew --dry-run` succeeds, `certbot.timer` enabled
- [ ] A nightly backup cron exists, copies off the box, and has a retention rule
- [ ] `./scripts/deploy.sh main backup-verify` succeeds
- [ ] `sudo ufw status` shows only 22, 80, 443
- [ ] `docker ps --format '{{.Ports}}'` shows no `0.0.0.0` publication
